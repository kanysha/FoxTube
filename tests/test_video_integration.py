import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, text
from test_auth_integration import AuthBrowser, expect_status, require

from app.auth.rate_limits import rate_key
from app.core.config import Settings
from app.core.db import make_engine

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["/api/v1/videos", "/search"])
def test_search_rejects_nul_before_database_access(path: str) -> None:
    base_url = os.environ.get("FOXTUBE_TEST_BASE_URL")
    if not base_url:
        pytest.fail("Use bash scripts/test.sh for isolated integration tests")
    with httpx.Client(base_url=base_url) as client:
        expect_status(client.get(path, params={"q": "bad\x00query"}), 422)


@dataclass(repr=False)
class VideoAccounts:
    owner: AuthBrowser = field(repr=False)
    grantee: AuthBrowser = field(repr=False)
    outsider: AuthBrowser = field(repr=False)
    guest: httpx.Client = field(repr=False)
    owner_user: dict[str, Any] = field(repr=False)
    grantee_user: dict[str, Any] = field(repr=False)
    outsider_user: dict[str, Any] = field(repr=False)
    database: Engine = field(repr=False)
    settings: Settings = field(repr=False)
    rate_baseline: tuple[str, ...] = field(repr=False)


@pytest.fixture(scope="module")
def video_accounts() -> Iterator[VideoAccounts]:
    base_url = os.environ.get("FOXTUBE_TEST_BASE_URL")
    if not base_url:
        pytest.fail("Use bash scripts/test.sh for isolated video integration tests")
    settings = Settings()
    engine = make_engine(settings)
    clients: list[httpx.Client] = []
    users: list[dict[str, Any]] = []

    def registered(name: str) -> tuple[AuthBrowser, dict[str, Any]]:
        client = httpx.Client(base_url=base_url, timeout=30)
        clients.append(client)
        browser = AuthBrowser(client, str(settings.public_origin).rstrip("/"))
        response = client.get("/api/v1/auth/session")
        expect_status(response, 200)
        browser.read_session(response)
        user = browser.register(display_name=name)
        users.append(user)
        return browser, user

    try:
        owner, owner_user = registered("Автор видеопроверки")
        grantee, grantee_user = registered("Приглашённый зритель")
        outsider, outsider_user = registered("Посторонний зритель")
        guest = httpx.Client(base_url=base_url, timeout=30)
        clients.append(guest)
        with engine.connect() as connection:
            rate_baseline = tuple(
                connection.scalars(text("SELECT key FROM auth_rate_limits"))
            )
        yield VideoAccounts(
            owner,
            grantee,
            outsider,
            guest,
            owner_user,
            grantee_user,
            outsider_user,
            engine,
            settings,
            rate_baseline,
        )
    finally:
        for client in clients:
            client.close()
        # These accounts exist only in the disposable Compose test project.
        if users:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"),
                    {"ids": [UUID(user["id"]) for user in users]},
                )
        engine.dispose()


@pytest.fixture(autouse=True)
def independent_video_rate_limits(video_accounts: VideoAccounts) -> Iterator[None]:
    def clear_video_limits() -> None:
        # Preserve pre-existing auth counters. Only the new counters produced
        # by this module's synthetic video requests are reset between scenarios.
        # Pytest runs these scenarios sequentially in a disposable test project.
        with video_accounts.database.begin() as connection:
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE NOT (key = ANY(:baseline))"),
                {"baseline": list(video_accounts.rate_baseline)},
            )

    clear_video_limits()
    try:
        yield
    finally:
        clear_video_limits()


def ffmpeg_fixture(path: Path, *, vertical: bool = False) -> bytes:
    dimensions = "288x512" if vertical else "320x180"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={dimensions}:rate=24:duration=10",
    ]
    if not vertical:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=10"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "2"]
    if not vertical:
        command += ["-c:a", "aac", "-b:a", "64k", "-shortest"]
    command += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, "The actual FFmpeg video fixture failed")
    return path.read_bytes()


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return ffmpeg_fixture(tmp_path_factory.mktemp("video-input") / "source.mp4")


def mutation_headers(browser: AuthBrowser) -> dict[str, str]:
    return {"Origin": browser.origin, "X-CSRF-Token": browser.csrf_token}


def create_video(
    accounts: VideoAccounts,
    size: int,
    *,
    visibility: str = "private",
    title: str | None = None,
) -> dict[str, Any]:
    response = accounts.owner.post(
        "/api/v1/videos",
        {
            "title": title or f"Реальная проверка {uuid4().hex}",
            "description": "Настоящий короткий файл; изолированные данные проверки.",
            "visibility": visibility,
            "expected_size": size,
        },
    )
    expect_status(response, 201)
    return dict(response.json())


def send_source(browser: AuthBrowser, video_id: str, data: bytes) -> httpx.Response:
    return browser.client.put(
        f"/api/v1/videos/{video_id}/source",
        content=data,
        headers={
            **mutation_headers(browser),
            "Content-Type": "application/octet-stream",
        },
    )


def await_state(
    browser: AuthBrowser, video_id: str, expected: str = "READY"
) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = browser.client.get(f"/api/v1/videos/{video_id}")
        expect_status(response, 200)
        video = dict(response.json())
        if video["status"] == expected:
            return video
        require(
            video["status"] not in {"READY", "FAILED"},
            f"Video reached {video['status']} instead of {expected}",
        )
        time.sleep(0.2)
    pytest.fail(f"Video did not reach {expected} within 90 seconds", pytrace=False)


def ready_video(
    accounts: VideoAccounts, data: bytes, *, visibility: str = "private"
) -> dict[str, Any]:
    video = create_video(accounts, len(data), visibility=visibility)
    expect_status(send_source(accounts.owner, video["id"], data), 202)
    return await_state(accounts.owner, video["id"])


def delete_video(accounts: VideoAccounts, video_id: str) -> None:
    response = accounts.owner.client.delete(
        f"/api/v1/videos/{video_id}", headers=mutation_headers(accounts.owner)
    )
    require(response.status_code in {204, 404}, "Test video cleanup failed")


@pytest.fixture
def private_video(
    video_accounts: VideoAccounts, sample_video: bytes
) -> Iterator[dict[str, Any]]:
    video = ready_video(video_accounts, sample_video)
    try:
        yield video
    finally:
        delete_video(video_accounts, video["id"])


def media_requests(client: httpx.Client, video_id: str, expected: int) -> None:
    for resource in ("playback", "poster"):
        path = f"/media/videos/{video_id}/{resource}"
        for method, headers in (
            ("GET", {}),
            ("HEAD", {}),
            ("GET", {"Range": "bytes=0-31"}),
        ):
            response = client.request(method, path, headers=headers)
            wanted = 206 if expected == 200 and headers else expected
            expect_status(response, wanted)
            if method == "HEAD":
                require(not response.content, "HEAD unexpectedly returned a body")
            if expected == 200:
                require(
                    "no-store" in response.headers.get("cache-control", ""),
                    "Protected media must not survive access revocation in caches",
                )


def assert_not_discoverable(client: httpx.Client, video: dict[str, Any]) -> None:
    expect_status(client.get(f"/api/v1/videos/{video['id']}"), 404)
    response = client.get("/api/v1/videos", params={"q": video["title"]})
    expect_status(response, 200)
    data = response.json()
    require(data["total"] == 0 and data["items"] == [], "List leaked a private video")
    page = client.get("/search", params={"q": video["title"]})
    expect_status(page, 200)
    require(f"/watch/{video['id']}" not in page.text, "Search leaked a private video")


def test_real_upload_processing_public_playback_and_range(
    video_accounts: VideoAccounts, sample_video: bytes, tmp_path: Path
) -> None:
    video = ready_video(video_accounts, sample_video, visibility="public")
    try:
        require(9.5 <= video["duration"] <= 10.5, "Incorrect output duration")
        require((video["width"], video["height"]) == (320, 180), "Video was upscaled")
        response = video_accounts.guest.get(video["playback_url"])
        expect_status(response, 200)
        require(
            response.headers["content-type"].startswith("video/mp4"), "Wrong MP4 MIME"
        )
        path = tmp_path / "result.mp4"
        path.write_bytes(response.content)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        require(probe.returncode == 0, "Published output cannot be decoded by ffprobe")
        streams = json.loads(probe.stdout)["streams"]
        picture = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        require(picture["codec_name"] == "h264", "Output video is not H.264")
        require(
            picture["pix_fmt"] == "yuv420p", "Output pixel format is not compatible"
        )
        require(audio["codec_name"] == "aac" and audio["channels"] <= 2, "Wrong audio")
        require(
            0 < response.content.find(b"moov") < response.content.find(b"mdat"),
            "MP4 metadata must precede media for faststart",
        )
        media_requests(video_accounts.guest, video["id"], 200)
        poster = video_accounts.guest.get(video["poster_url"])
        require(poster.content.startswith(b"\xff\xd8\xff"), "Poster is not JPEG")
        length = len(response.content)
        partial = video_accounts.guest.get(
            video["playback_url"], headers={"Range": "bytes=32-95"}
        )
        expect_status(partial, 206)
        require(
            partial.content == response.content[32:96], "Range returned wrong bytes"
        )
        require(
            partial.headers.get("content-range") == f"bytes 32-95/{length}",
            "Range returned an incorrect Content-Range",
        )
        past_end = video_accounts.guest.get(
            video["playback_url"], headers={"Range": f"bytes={length}-"}
        )
        expect_status(past_end, 416)
        require(
            past_end.headers.get("content-range") == f"bytes */{length}",
            "Unsatisfiable range must include the actual complete size",
        )
        for value in ("nonsense", "unknown=0-4", "bytes=not-a-range"):
            malformed = video_accounts.guest.get(
                video["playback_url"], headers={"Range": value}
            )
            require(
                malformed.status_code < 500, "Malformed Range caused a server error"
            )
        with video_accounts.database.connect() as connection:
            source_key = connection.scalar(
                text("SELECT source_key FROM videos WHERE id = :id"),
                {"id": UUID(video["id"])},
            )
        if source_key:
            require(
                not (video_accounts.settings.media_root / source_key).exists(),
                "Source remained on disk after successful publication",
            )
        expect_status(video_accounts.guest.get(f"/watch/{video['id']}"), 200)
    finally:
        delete_video(video_accounts, video["id"])


def test_silent_vertical_video_is_processed_without_audio_requirement(
    video_accounts: VideoAccounts, tmp_path: Path
) -> None:
    data = ffmpeg_fixture(tmp_path / "vertical.mp4", vertical=True)
    video = ready_video(video_accounts, data)
    try:
        require(video["height"] > video["width"], "Vertical aspect was lost")
        require(video["width"] <= 288 and video["height"] <= 512, "Video was upscaled")
        response = video_accounts.owner.client.get(video["playback_url"])
        expect_status(response, 200)
        output = tmp_path / "vertical-result.mp4"
        output.write_bytes(response.content)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        require(probe.returncode == 0, "Silent output is invalid")
        streams = json.loads(probe.stdout)["streams"]
        require(
            [stream["codec_type"] for stream in streams] == ["video"],
            "Silent input unexpectedly acquired an audio stream",
        )
    finally:
        delete_video(video_accounts, video["id"])


def test_private_video_acl_includes_privileged_roles_and_every_media_method(
    video_accounts: VideoAccounts, private_video: dict[str, Any]
) -> None:
    video_id = private_video["id"]
    media_requests(video_accounts.owner.client, video_id, 200)
    assert_not_discoverable(video_accounts.guest, private_video)
    media_requests(video_accounts.guest, video_id, 404)
    try:
        for role in ("user", "moderator", "admin"):
            with video_accounts.database.begin() as connection:
                connection.execute(
                    text("UPDATE users SET role = :role WHERE id = :id"),
                    {"role": role, "id": UUID(video_accounts.outsider_user["id"])},
                )
            assert_not_discoverable(video_accounts.outsider.client, private_video)
            media_requests(video_accounts.outsider.client, video_id, 404)
    finally:
        with video_accounts.database.begin() as connection:
            connection.execute(
                text("UPDATE users SET role = 'user' WHERE id = :id"),
                {"id": UUID(video_accounts.outsider_user["id"])},
            )


def test_owner_grant_duplicate_and_revoke_close_new_media_requests(
    video_accounts: VideoAccounts, private_video: dict[str, Any]
) -> None:
    path = f"/api/v1/videos/{private_video['id']}/grants"
    email = f"  {video_accounts.grantee_user['email'].upper()}  "
    for _ in range(2):
        expect_status(video_accounts.owner.post(path, {"email": email}), 204)
    response = video_accounts.owner.client.get(path)
    expect_status(response, 200)
    grants = response.json()["items"]
    require(len(grants) == 1, "Repeated grants created duplicate access entries")
    require(
        grants[0]["user_id"] == video_accounts.grantee_user["id"],
        "Grant does not reference the registered account",
    )
    expect_status(
        video_accounts.grantee.client.get(f"/api/v1/videos/{private_video['id']}"), 200
    )
    media_requests(video_accounts.grantee.client, private_video["id"], 200)
    listed = video_accounts.grantee.client.get(
        "/api/v1/videos", params={"q": private_video["title"]}
    )
    expect_status(listed, 200)
    require(
        listed.json()["total"] == 1, "Granted private video missing from allowed list"
    )
    expect_status(video_accounts.outsider.post(path, {"email": email}), 404)
    expect_status(video_accounts.outsider.client.get(path), 404)
    expect_status(
        video_accounts.owner.post(
            path, {"email": f"missing-{uuid4().hex}@example.com"}
        ),
        422,
    )
    response = video_accounts.owner.client.delete(
        f"{path}/{video_accounts.grantee_user['id']}",
        headers=mutation_headers(video_accounts.owner),
    )
    expect_status(response, 204)
    assert_not_discoverable(video_accounts.grantee.client, private_video)
    media_requests(video_accounts.grantee.client, private_video["id"], 404)


def test_visibility_hidden_and_deleted_never_bypass_acl(
    video_accounts: VideoAccounts, private_video: dict[str, Any]
) -> None:
    video_id = private_video["id"]
    path = f"/api/v1/videos/{video_id}"
    response = video_accounts.owner.client.patch(
        path,
        json={"visibility": "public"},
        headers=mutation_headers(video_accounts.owner),
    )
    expect_status(response, 200)
    media_requests(video_accounts.guest, video_id, 200)
    response = video_accounts.owner.client.patch(
        path,
        json={"visibility": "private"},
        headers=mutation_headers(video_accounts.owner),
    )
    expect_status(response, 200)
    media_requests(video_accounts.guest, video_id, 404)
    with video_accounts.database.begin() as connection:
        connection.execute(
            text("UPDATE videos SET moderation_status = 'hidden' WHERE id = :id"),
            {"id": UUID(video_id)},
        )
    for client in (video_accounts.owner.client, video_accounts.guest):
        media_requests(client, video_id, 404)
    assert_not_discoverable(video_accounts.guest, private_video)
    delete_video(video_accounts, video_id)
    for client in (video_accounts.owner.client, video_accounts.guest):
        expect_status(client.get(path), 404)
        media_requests(client, video_id, 404)


def status_before_body(base_url: httpx.URL, path: str, headers: dict[str, str]) -> int:
    # Send only headers for a nonempty body. A response proves that the app did
    # not wait for body bytes before checking the session, CSRF or object owner.
    host, port = base_url.host, base_url.port or 80
    lines = [
        f"PUT {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: close",
        "Content-Type: application/octet-stream",
        "Content-Length: 4096",
        *[f"{name}: {value}" for name, value in headers.items()],
        "\r\n",
    ]
    try:
        with socket.create_connection((host, port), timeout=5) as stream:
            stream.sendall("\r\n".join(lines).encode("ascii"))
            response = stream.recv(4096)
    except (OSError, ValueError):
        pytest.fail(
            "Upload authorization waited for an unsent request body", pytrace=False
        )
    try:
        return int(response.split(b"\r\n", 1)[0].split()[1])
    except (ValueError, IndexError):
        pytest.fail(
            "Upload authorization returned an invalid HTTP response", pytrace=False
        )


def test_upload_authentication_csrf_and_owner_are_checked_before_body(
    video_accounts: VideoAccounts,
) -> None:
    video = create_video(video_accounts, 4096)
    path = f"/api/v1/videos/{video['id']}/source"
    try:
        require(
            status_before_body(video_accounts.guest.base_url, path, {}) == 401,
            "Anonymous upload must be rejected before reading the body",
        )
        owner_headers = {"Cookie": f"foxtube_session={video_accounts.owner.cookie()}"}
        require(
            status_before_body(video_accounts.guest.base_url, path, owner_headers)
            == 403,
            "Missing CSRF must be rejected before reading the body",
        )
        outsider_headers = {
            "Cookie": f"foxtube_session={video_accounts.outsider.cookie()}",
            **mutation_headers(video_accounts.outsider),
        }
        require(
            status_before_body(video_accounts.guest.base_url, path, outsider_headers)
            == 404,
            "Another user's upload must be rejected before reading the body",
        )
        with video_accounts.database.connect() as connection:
            jobs = connection.scalar(
                text("SELECT count(*) FROM video_jobs WHERE video_id = :id"),
                {"id": UUID(video["id"])},
            )
        require(jobs == 0, "Rejected requests created a processing job")
    finally:
        delete_video(video_accounts, video["id"])


def test_actual_upload_bytes_cannot_exceed_reserved_size(
    video_accounts: VideoAccounts, sample_video: bytes
) -> None:
    video = create_video(video_accounts, len(sample_video))
    try:
        response = video_accounts.owner.client.put(
            f"/api/v1/videos/{video['id']}/source",
            content=iter([sample_video, b"exceeds-reserved-size"]),
            headers={
                **mutation_headers(video_accounts.owner),
                "Content-Type": "application/octet-stream",
            },
        )
        expect_status(response, 413)
        with video_accounts.database.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM video_jobs WHERE video_id = :id"),
                {"id": UUID(video["id"])},
            )
            reservation = connection.execute(
                text("SELECT reserved_bytes, upload_token FROM videos WHERE id = :id"),
                {"id": UUID(video["id"])},
            ).one()
        require(count == 0, "Oversized body was queued despite its smaller reservation")
        require(
            reservation.reserved_bytes == 0 and reservation.upload_token is None,
            "Oversized body retained the quota reservation or active upload slot",
        )
        media_requests(video_accounts.owner.client, video["id"], 404)
        incoming = video_accounts.settings.media_root / "incoming" / video["id"]
        require(not list(incoming.glob("*.part")), "Failed upload left a partial file")
        # A full-file retry must obtain a fresh reservation and then successfully
        # process the original real video, rather than retain the aborted slot.
        expect_status(send_source(video_accounts.owner, video["id"], sample_video), 202)
        await_state(video_accounts.owner, video["id"])
    finally:
        delete_video(video_accounts, video["id"])


def test_one_unfinished_draft_per_owner_does_not_duplicate_reservations(
    video_accounts: VideoAccounts,
) -> None:
    video = create_video(video_accounts, 1024)
    try:
        with video_accounts.database.connect() as connection:
            original_reservation = connection.scalar(
                text("SELECT reserved_bytes FROM videos WHERE id = :id"),
                {"id": UUID(video["id"])},
            )
        response = video_accounts.owner.post(
            "/api/v1/videos",
            {
                "title": "Второй незавершённый черновик",
                "description": "",
                "visibility": "private",
                "expected_size": 1024,
            },
        )
        expect_status(response, 409)
        with video_accounts.database.connect() as connection:
            count, total_reserved = connection.execute(
                text(
                    "SELECT count(*), sum(reserved_bytes) FROM videos "
                    "WHERE owner_id = :owner AND deleted_at IS NULL "
                    "AND status = 'UPLOADING'"
                ),
                {"owner": UUID(video_accounts.owner_user["id"])},
            ).one()
        require(count == 1, "Rejected second draft created an extra unfinished video")
        require(
            original_reservation > 0 and total_reserved == original_reservation,
            "Rejected second draft changed the original storage reservation",
        )
    finally:
        delete_video(video_accounts, video["id"])


def test_draft_creation_rate_limit_counts_rejected_attempts(
    video_accounts: VideoAccounts,
) -> None:
    video = create_video(video_accounts, 1024)
    try:
        payload = {
            "title": "Ограничение частоты создания",
            "description": "",
            "visibility": "private",
            "expected_size": 1024,
        }
        # The first creation succeeded. Nine more attempts hit the single-draft
        # guard, but must still consume the shared per-user request allowance.
        for _ in range(9):
            expect_status(video_accounts.owner.post("/api/v1/videos", payload), 409)
        response = video_accounts.owner.post("/api/v1/videos", payload)
        expect_status(response, 429)
        retry_after = response.headers.get("retry-after", "")
        require(
            retry_after.isdecimal() and 0 < int(retry_after) <= 900,
            "Video rate limit must return a bounded Retry-After",
        )
        with video_accounts.database.connect() as connection:
            attempts = connection.scalar(
                text("SELECT attempts FROM auth_rate_limits WHERE key = :key"),
                {"key": rate_key("video-user", video_accounts.owner_user["id"])},
            )
        require(
            attempts == 11, "Rejected draft attempts were not committed to the limit"
        )
    finally:
        delete_video(video_accounts, video["id"])


def test_repeated_accepted_put_is_idempotent(
    video_accounts: VideoAccounts, sample_video: bytes
) -> None:
    video = create_video(video_accounts, len(sample_video))
    try:
        expect_status(send_source(video_accounts.owner, video["id"], sample_video), 202)
        expect_status(send_source(video_accounts.owner, video["id"], sample_video), 202)
        ready = await_state(video_accounts.owner, video["id"])
        require(ready["status"] == "READY", "Repeated upload disturbed processing")
        with video_accounts.database.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM video_jobs WHERE video_id = :id"),
                {"id": UUID(video["id"])},
            )
        require(count == 1, "Repeated accepted upload created duplicate jobs")
    finally:
        delete_video(video_accounts, video["id"])


def test_non_video_source_fails_without_publishing(
    video_accounts: VideoAccounts,
) -> None:
    data = b"This is deliberately not a video, regardless of its filename."
    video = create_video(video_accounts, len(data), visibility="public")
    try:
        expect_status(send_source(video_accounts.owner, video["id"], data), 202)
        result = await_state(video_accounts.owner, video["id"], "FAILED")
        require(
            bool(result["error"]), "A failed upload must explain the failure safely"
        )
        require(result["playback_url"] is None, "Failed upload exposes playback URL")
        require(result["poster_url"] is None, "Failed upload exposes poster URL")
        require(
            "/var/" not in result["error"], "Failure reveals private filesystem paths"
        )
        media_requests(video_accounts.owner.client, video["id"], 404)
        assert_not_discoverable(video_accounts.guest, video)
        with video_accounts.database.connect() as connection:
            attempts = connection.scalar(
                text("SELECT attempts FROM video_jobs WHERE video_id = :id"),
                {"id": UUID(video["id"])},
            )
        require(attempts == 1, "Invalid media must not consume three transient retries")
    finally:
        delete_video(video_accounts, video["id"])


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_size": 0},
        {"expected_size": 500 * 1024 * 1024 + 1},
        {"title": "   "},
        {"title": "Заголовок\x00"},
        {"title": "Заголовок\ud800"},
        {"description": "Описание\x00"},
        {"description": "Описание\ud800"},
        {"visibility": "unlisted"},
        {"status": "READY"},
        {"owner_id": str(uuid4())},
        {"playback_key": "../../secret"},
        {"moderation_status": "visible"},
    ],
)
def test_upload_metadata_rejects_invalid_limits_and_mass_assignment(
    video_accounts: VideoAccounts, changes: dict[str, Any]
) -> None:
    payload: dict[str, Any] = {
        "title": "Неверные метаданные",
        "description": "Проверка границы входной схемы.",
        "visibility": "private",
        "expected_size": 1024,
    }
    payload.update(changes)
    expect_status(
        video_accounts.owner.client.post(
            "/api/v1/videos",
            content=json.dumps(payload),
            headers={
                **mutation_headers(video_accounts.owner),
                "Content-Type": "application/json",
            },
        ),
        422,
    )


def test_foreign_owner_mutations_cannot_change_or_delete_a_video(
    video_accounts: VideoAccounts, private_video: dict[str, Any]
) -> None:
    browser = video_accounts.outsider
    path = f"/api/v1/videos/{private_video['id']}"
    expect_status(
        browser.client.patch(
            path, json={"visibility": "public"}, headers=mutation_headers(browser)
        ),
        404,
    )
    expect_status(browser.client.delete(path, headers=mutation_headers(browser)), 404)
    expect_status(video_accounts.owner.client.get(path), 200)
    assert_not_discoverable(video_accounts.guest, private_video)
