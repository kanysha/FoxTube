import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.models import ServiceHeartbeat
from app.core.readiness import WORKER_NAME
from app.videos import limits
from app.videos.models import Video, VideoJob
from app.videos.service import capacity_lock
from app.videos.storage import media_path, sync_directory
from app.videos.transcode import (
    InvalidVideo,
    TranscodeInterrupted,
    VideoInfo,
    transcode,
)

logger = logging.getLogger("foxtube.video")


@dataclass(frozen=True)
class Attempt:
    video_id: UUID
    token: UUID
    source_key: str
    number: int

    @property
    def work_key(self) -> str:
        return f"work/{self.video_id}/{self.token}"

    @property
    def published_key(self) -> str:
        return f"videos/{self.video_id}/{self.token}"


def claim_job(engine: Engine) -> Attempt | None:
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        job = session.scalar(
            select(VideoJob)
            .join(Video, Video.id == VideoJob.video_id)
            .where(
                Video.deleted_at.is_(None),
                Video.source_key.is_not(None),
                or_(
                    and_(VideoJob.status == "queued", VideoJob.available_at <= now),
                    and_(
                        VideoJob.status == "running", VideoJob.lease_expires_at <= now
                    ),
                ),
            )
            .order_by(VideoJob.available_at, VideoJob.video_id)
            .with_for_update(skip_locked=True, of=VideoJob)
            .limit(1)
        )
        if job is None:
            return None
        video = session.scalar(
            select(Video).where(Video.id == job.video_id).with_for_update()
        )
        assert video is not None
        if job.attempts >= limits.MAX_ATTEMPTS:
            job.status = "failed"
            job.lease_expires_at = None
            video.status = "FAILED"
            video.error = "Обработка прервана несколько раз. Загрузите видео заново."
            video.reserved_bytes = 0
            video.updated_at = now
            return None
        job.attempts += 1
        job.status = "running"
        job.attempt_token = uuid4()
        job.lease_expires_at = now + timedelta(seconds=limits.JOB_LEASE_SECONDS)
        video.status = "PROCESSING"
        video.updated_at = now
        assert video.source_key is not None
        return Attempt(video.id, job.attempt_token, video.source_key, job.attempts)


def active_attempt(
    session: Session, attempt: Attempt, require_lease: bool = True
) -> tuple[VideoJob, Video]:
    job = session.scalar(
        select(VideoJob).where(VideoJob.video_id == attempt.video_id).with_for_update()
    )
    video = session.scalar(
        select(Video).where(Video.id == attempt.video_id).with_for_update()
    )
    if (
        job is None
        or video is None
        or job.status != "running"
        or job.attempt_token != attempt.token
        or video.deleted_at is not None
        or video.status != "PROCESSING"
        or video.source_key != attempt.source_key
        or (
            require_lease
            and (
                job.lease_expires_at is None
                or job.lease_expires_at <= datetime.now(UTC)
            )
        )
    ):
        raise TranscodeInterrupted("Processing lease lost")
    return job, video


def renew_attempt(engine: Engine, attempt: Attempt) -> None:
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        job, _video = active_attempt(session, attempt)
        job.lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=limits.JOB_LEASE_SECONDS
        )
        statement = insert(ServiceHeartbeat).values(
            name=WORKER_NAME, last_seen=func.clock_timestamp()
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[ServiceHeartbeat.name],
                set_={"last_seen": func.clock_timestamp()},
            )
        )


def finish_attempt(
    engine: Engine, attempt: Attempt, info: VideoInfo, output_bytes: int
) -> None:
    if output_bytes <= 0 or output_bytes > limits.OUTPUT_RESERVATION:
        raise InvalidVideo("Размер результата превышает допустимый предел.")
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        job, video = active_attempt(session, attempt)
        video.status = "READY"
        video.playback_key = f"{attempt.published_key}/playback.mp4"
        video.poster_key = f"{attempt.published_key}/poster.jpg"
        video.duration, video.width, video.height = (
            info.duration,
            info.width,
            info.height,
        )
        video.storage_bytes = video.expected_size + output_bytes
        video.reserved_bytes = 0
        video.updated_at = datetime.now(UTC)
        video.error = None
        job.status = "done"
        job.lease_expires_at = None


def fail_attempt(engine: Engine, attempt: Attempt, error: str, transient: bool) -> None:
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        try:
            job, video = active_attempt(session, attempt, require_lease=False)
        except TranscodeInterrupted:
            return
        retry = transient and job.attempts < limits.MAX_ATTEMPTS
        job.status = "queued" if retry else "failed"
        job.available_at = datetime.now(UTC) + timedelta(seconds=10 * job.attempts)
        job.lease_expires_at = None
        video.status = "QUEUED" if retry else "FAILED"
        video.error = None if retry else error[:300]
        video.updated_at = datetime.now(UTC)
        if not retry:
            video.reserved_bytes = 0


def remove_ready_source(engine: Engine, root: Path, video_id: UUID) -> None:
    # Read snapshot -> remove immutable source -> short conditional accounting.
    with Session(engine) as session:
        row = session.execute(
            select(Video.source_key, Video.playback_key, Video.poster_key).where(
                Video.id == video_id, Video.status == "READY"
            )
        ).first()
    if row is None or row[0] is None:
        return
    source, playback, poster = row
    assert source is not None and playback is not None and poster is not None
    media_path(root, source).unlink(missing_ok=True)
    size = (
        media_path(root, playback).stat().st_size
        + media_path(root, poster).stat().st_size
    )
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        video = session.scalar(
            select(Video).where(Video.id == video_id).with_for_update()
        )
        if video and video.status == "READY" and video.source_key == source:
            video.source_key = None
            video.storage_bytes = size


def process_one(engine: Engine, root: Path, stop: threading.Event) -> bool:
    attempt = claim_job(engine)
    if attempt is None:
        return False
    work = media_path(root, attempt.work_key)
    published = media_path(root, attempt.published_key)
    next_heartbeat = 0.0

    def checkpoint() -> None:
        nonlocal next_heartbeat
        if stop.is_set():
            raise TranscodeInterrupted("Worker stopping")
        if time.monotonic() >= next_heartbeat:
            try:
                renew_attempt(engine, attempt)
            except SQLAlchemyError as error:
                raise TranscodeInterrupted("Database unavailable") from error
            next_heartbeat = time.monotonic() + 5

    try:
        work.mkdir(parents=True, exist_ok=False)
        info = transcode(media_path(root, attempt.source_key), work, checkpoint)
        output_bytes = 0
        for name in ("playback.mp4", "poster.jpg"):
            path = media_path(root, f"{attempt.work_key}/{name}")
            output_bytes += path.stat().st_size
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        next_heartbeat = 0
        checkpoint()
        published.parent.mkdir(parents=True, exist_ok=True)
        os.replace(work, published)
        sync_directory(published.parent)
        finish_attempt(engine, attempt, info, output_bytes)
        remove_ready_source(engine, root, attempt.video_id)
        logger.info("Video processing completed")
    except InvalidVideo as error:
        fail_attempt(engine, attempt, str(error), transient=False)
        logger.info("Video rejected by validation")
    except (OSError, SQLAlchemyError, TranscodeInterrupted):
        # Unknown COMMIT outcome: never delete published files here. The sweep
        # checks the database before deleting an old, unreferenced attempt.
        try:
            fail_attempt(
                engine,
                attempt,
                "Обработка временно недоступна. Загрузите видео заново.",
                transient=True,
            )
        except SQLAlchemyError:
            pass
        logger.warning("Video attempt interrupted; lease/retry will recover it")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return True


def cleanup_media(engine: Engine, root: Path) -> None:
    """Bounded reconciliation; no SQL transaction is held while deleting files."""
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        expired = session.scalars(
            select(Video)
            .where(
                Video.status == "UPLOADING",
                or_(
                    Video.reservation_expires_at <= now,
                    and_(
                        Video.upload_token.is_not(None), Video.upload_expires_at <= now
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
            .limit(100)
        ).all()
        for video in expired:
            video.status = "FAILED"
            video.error = "Срок загрузки истёк. Загрузите видео заново."
            video.reserved_bytes = 0
            video.upload_token = None
            video.upload_expires_at = None
            video.updated_at = now
        candidates = list(
            session.scalars(
                select(Video.id)
                .where(
                    or_(
                        Video.deleted_at.is_not(None),
                        and_(
                            Video.status == "FAILED",
                            Video.updated_at < now - timedelta(hours=24),
                        ),
                    ),
                    or_(
                        Video.storage_bytes > 0,
                        Video.reserved_bytes > 0,
                        Video.source_key.is_not(None),
                        Video.playback_key.is_not(None),
                    ),
                )
                .limit(100)
            )
        )
        ready_sources = list(
            session.scalars(
                select(Video.id)
                .where(
                    Video.status == "READY",
                    Video.source_key.is_not(None),
                    Video.deleted_at.is_(None),
                )
                .limit(100)
            )
        )
    for video_id in candidates:
        for prefix in ("incoming", "sources", "work", "videos"):
            directory = media_path(root, f"{prefix}/{video_id}")
            if directory.exists():
                shutil.rmtree(directory)
        with Session(engine) as session, session.begin():
            capacity_lock(session)
            job = session.scalar(
                select(VideoJob).where(VideoJob.video_id == video_id).with_for_update()
            )
            candidate = session.scalar(
                select(Video).where(Video.id == video_id).with_for_update()
            )
            if candidate and (
                candidate.deleted_at is not None
                or (
                    candidate.status == "FAILED"
                    and candidate.updated_at < now - timedelta(hours=24)
                )
            ):
                candidate.status = "FAILED"
                candidate.storage_bytes = candidate.reserved_bytes = 0
                candidate.source_key = candidate.playback_key = candidate.poster_key = (
                    None
                )
                candidate.upload_token = None
                candidate.upload_expires_at = None
                if job:
                    job.status = "failed"
                    job.lease_expires_at = None
    for video_id in ready_sources:
        try:
            remove_ready_source(engine, root, video_id)
        except OSError:
            logger.warning("Source cleanup deferred")
    sweep_orphans(engine, root, now)


_sweep_cursors: dict[Path, Iterator[Path]] = {}


def orphan_entries(root: Path) -> Iterator[Path]:
    for prefix in ("incoming", "sources", "work", "videos"):
        directory = media_path(root, prefix)
        if not directory.exists():
            continue
        for parent in directory.iterdir():
            if not parent.is_dir() or parent.is_symlink():
                continue
            try:
                yield from parent.iterdir()
                parent.rmdir()  # Only succeeds for an empty directory.
            except (FileNotFoundError, OSError):
                continue


def sweep_orphans(engine: Engine, root: Path, now: datetime) -> None:
    # Keep the iterator between passes: 200 live files must not starve later
    # garbage. Both the filesystem batch and its SQL snapshot stay bounded.
    cursor = _sweep_cursors.setdefault(root, orphan_entries(root))
    batch: list[Path] = []
    for _ in range(200):
        try:
            batch.append(next(cursor))
        except StopIteration:
            _sweep_cursors.pop(root, None)
            break
    ids: set[UUID] = set()
    for entry in batch:
        try:
            ids.add(UUID(entry.parent.name))
        except ValueError:
            continue
    with Session(engine) as session:
        rows = list(session.scalars(select(Video).where(Video.id.in_(ids))))
        keys = {
            key for row in rows for key in (row.source_key, row.playback_key) if key
        }
        active_uploads = {
            str(row.upload_token)
            for row in rows
            if row.upload_token
            and row.upload_expires_at
            and row.upload_expires_at > now
        }
        active_attempts = {
            str(token)
            for token in session.scalars(
                select(VideoJob.attempt_token).where(
                    VideoJob.video_id.in_(ids),
                    VideoJob.status == "running",
                    VideoJob.lease_expires_at > now,
                )
            )
        }
    for entry in batch:
        try:
            key = entry.relative_to(root).as_posix()
            if (
                entry.is_symlink()
                or now.timestamp() - entry.stat().st_mtime < 600
                or entry.stem in active_uploads
                or entry.name in active_attempts
                or key in keys
                or f"{key}/playback.mp4" in keys
            ):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
