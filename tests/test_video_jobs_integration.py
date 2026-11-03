import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.auth.models import Channel, User
from app.core.config import Settings
from app.core.db import Base, make_engine
from app.videos import limits, processing, service
from app.videos.models import Video, VideoJob
from app.videos.schemas import VideoInput
from app.videos.storage import media_path
from app.videos.transcode import TranscodeInterrupted, VideoInfo

pytestmark = pytest.mark.integration


@pytest.fixture
def queue_database() -> Iterator[tuple[Engine, UUID]]:
    if not os.environ.get("FOXTUBE_TEST_BASE_URL"):
        pytest.fail("Run with scripts/test.sh in the disposable PostgreSQL project")
    # A separate schema prevents the persistent worker from racing fault injection.
    # HTTP/ACL tests separately use the deployed schema and restricted roles.
    database = make_engine(Settings())
    schema = f"queue_test_{uuid4().hex}"
    engine = database.execution_options(schema_translate_map={None: schema})
    try:
        with database.begin() as connection:
            connection.execute(CreateSchema(schema))
        Base.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            user = User(
                email=f"{uuid4().hex}@example.com",
                password_hash="unused-test-hash",
                display_name="Автор очереди",
            )
            session.add(user)
            session.flush()
            owner = user.id
            session.add(
                Channel(user_id=owner, handle=uuid4().hex, title="Тестовый канал")
            )
        yield engine, owner
    finally:
        with database.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        database.dispose()


def queued(engine: Engine, owner: UUID, root: Path, *, hidden: bool = False) -> UUID:
    video_id = uuid4()
    key = f"sources/{video_id}/{uuid4()}.source"
    source = media_path(root, key)
    source.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mp4",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    with Session(engine) as session, session.begin():
        session.add(
            Video(
                id=video_id,
                owner_id=owner,
                title="Реальный исходник",
                visibility="private",
                moderation_status="hidden" if hidden else "visible",
                status="QUEUED",
                expected_size=source.stat().st_size,
                storage_bytes=source.stat().st_size,
                reserved_bytes=limits.OUTPUT_RESERVATION,
                source_key=key,
                reservation_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.flush()
        session.add(VideoJob(video_id=video_id))
    return video_id


def expire(engine: Engine, video_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        job = session.get(VideoJob, video_id)
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


def test_concurrent_claim_has_one_winner(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    queued(engine, owner, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: processing.claim_job(engine), range(2)))
    assert sum(attempt is not None for attempt in results) == 1


def test_expired_attempt_cannot_publish_or_renew(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path, hidden=True)
    old = processing.claim_job(engine)
    assert old is not None
    expire(engine, video_id)
    current = processing.claim_job(engine)
    assert current is not None and current.token != old.token and current.number == 2
    with pytest.raises(TranscodeInterrupted):
        processing.finish_attempt(engine, old, VideoInfo(4, 320, 180, False), 1000)
    with pytest.raises(TranscodeInterrupted):
        processing.renew_attempt(engine, old)
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video is not None
        assert (
            video.status,
            video.visibility,
            video.moderation_status,
            video.playback_key,
        ) == ("PROCESSING", "private", "hidden", None)


def test_expired_worker_recovers_real_video_without_changing_privacy(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path, hidden=True)
    assert processing.claim_job(engine) is not None
    expire(engine, video_id)
    assert processing.process_one(engine, tmp_path, threading.Event())
    with Session(engine) as session:
        video = session.get(Video, video_id)
        job = session.get(VideoJob, video_id)
        assert video is not None and job is not None
        assert (video.status, video.visibility, video.moderation_status) == (
            "READY",
            "private",
            "hidden",
        )
        assert video.source_key is None and video.reserved_bytes == 0
        assert (
            video.playback_key
            and media_path(tmp_path, video.playback_key).stat().st_size > 0
        )
        assert job.attempts == 2 and job.status == "done"


def test_stop_requeues_and_retry_processes_source(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path)
    stopped = threading.Event()
    stopped.set()
    assert processing.process_one(engine, tmp_path, stopped)
    with Session(engine) as session, session.begin():
        video = session.get(Video, video_id)
        job = session.get(VideoJob, video_id)
        assert video is not None and job is not None
        assert video.status == "QUEUED" and job.status == "queued" and job.attempts == 1
        assert video.source_key and media_path(tmp_path, video.source_key).is_file()
        job.available_at = datetime.now(UTC)
    assert processing.process_one(engine, tmp_path, threading.Event())
    with Session(engine) as session:
        assert (
            session.scalar(select(Video.status).where(Video.id == video_id)) == "READY"
        )


def test_retry_budget_is_finite(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path)
    for number in range(1, 4):
        attempt = processing.claim_job(engine)
        assert attempt is not None and attempt.number == number
        expire(engine, video_id)
    assert processing.claim_job(engine) is None
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video and video.status == "FAILED" and video.reserved_bytes == 0


def test_delete_during_attempt_prevents_publication(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path)
    attempt = processing.claim_job(engine)
    assert attempt is not None
    service.delete_video(engine, video_id, owner)
    with pytest.raises(TranscodeInterrupted):
        processing.finish_attempt(engine, attempt, VideoInfo(4, 320, 180, False), 1000)
    processing.cleanup_media(engine, tmp_path)
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert (
            video
            and video.deleted_at
            and video.storage_bytes == video.reserved_bytes == 0
        )
        assert video.playback_key is None
    assert not (tmp_path / "sources" / str(video_id)).exists()


def test_cleanup_failure_preserves_quota_accounting(
    queue_database: tuple[Engine, UUID], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner = queue_database
    video_id = queued(engine, owner, tmp_path)
    service.delete_video(engine, video_id, owner)

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("controlled deletion failure")

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(PermissionError):
        processing.cleanup_media(engine, tmp_path)
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video and video.storage_bytes > 0 and video.reserved_bytes > 0


def test_orphans_after_200_live_entries_are_eventually_removed(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    old = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    protected: list[Path] = []
    with Session(engine) as session, session.begin():
        for _ in range(200):
            video_id = uuid4()
            key = f"sources/{video_id}/{uuid4()}.source"
            path = media_path(tmp_path, key)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"retained source")
            os.utime(path, (old, old))
            protected.append(path)
            session.add(
                Video(
                    id=video_id,
                    owner_id=owner,
                    title="Сохранённый источник",
                    status="FAILED",
                    expected_size=15,
                    storage_bytes=15,
                    source_key=key,
                    reservation_expires_at=datetime.now(UTC),
                )
            )
    orphan = media_path(tmp_path, f"videos/{uuid4()}/{uuid4()}")
    orphan.mkdir(parents=True)
    (orphan / "playback.mp4").write_bytes(b"abandoned publication")
    os.utime(orphan, (old, old))
    for _ in range(4):
        processing.sweep_orphans(engine, tmp_path, datetime.now(UTC))
    assert not orphan.exists()
    assert all(path.is_file() for path in protected)


def test_quota_rejected_before_new_reservation(
    queue_database: tuple[Engine, UUID], tmp_path: Path
) -> None:
    engine, owner = queue_database
    with Session(engine) as session, session.begin():
        session.add(
            Video(
                owner_id=owner,
                title="Квота занята",
                status="FAILED",
                expected_size=1,
                storage_bytes=limits.USER_QUOTA,
                reservation_expires_at=datetime.now(UTC),
            )
        )
    with pytest.raises(HTTPException) as rejected:
        service.create_video(
            engine, tmp_path, owner, VideoInput(title="Новая загрузка", expected_size=1)
        )
    assert rejected.value.status_code == 413
    with Session(engine) as session:
        assert len(list(session.scalars(select(Video.id)))) == 1
