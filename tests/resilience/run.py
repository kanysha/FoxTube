"""Real container failures against disposable Compose resources, using stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://nginx:8080"


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        expected: int = 200,
    ) -> tuple[bytes, Any]:
        request_headers = dict(headers or {})
        if method not in {"GET", "HEAD"}:
            request_headers.update({"Origin": ORIGIN, "X-CSRF-Token": self.csrf})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=request_headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            assert response.status == expected, (
                f"{method} {path}: expected {expected}, received {response.status}"
            )
            return body, response.headers

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        body, _ = self.request(method, path, **kwargs)
        return json.loads(body)

    def register(self, email: str) -> None:
        self.csrf = self.json("GET", "/api/v1/auth/session")["csrf_token"]
        state = self.json(
            "POST",
            "/api/v1/auth/register",
            expected=201,
            data={
                "email": email,
                "password": secrets.token_urlsafe(24),
                "display_name": "Аварийная проверка",
            },
        )
        self.csrf = state["csrf_token"]


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        assert args.project.startswith("foxtube-resilience-")
        self.args = args
        self.command = [
            "docker",
            "compose",
            "--project-name",
            args.project,
            "--env-file",
            str(args.env_file),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose.test.yaml"),
            "--profile",
            "test",
        ]
        self.report: dict[str, Any] = {
            "status": "running",
            "environment": "isolated Docker/PostgreSQL/Nginx/FFmpeg",
            "checks": [],
            "limitations": [
                "Not a clean Ubuntu installation or a target-capacity load test.",
                "Database failure is a stopped container, not disk corruption.",
                "Previously received bytes and open transfers cannot be revoked.",
            ],
        }
        self.write_report()

    def compose(
        self, *args: str, stdin: bytes | None = None, timeout: int = 180
    ) -> bytes:
        result = subprocess.run(
            [*self.command, *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        assert result.returncode == 0, f"Compose {args[0]} failed ({result.returncode})"
        return result.stdout

    def sql(self, statement: str) -> str:
        return (
            self.compose(
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                "exec psql --no-psqlrc --set=ON_ERROR_STOP=1 -tA "
                '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB"',
                stdin=statement.encode(),
                timeout=12,
            )
            .decode()
            .strip()
        )

    def snapshot(self, video_id: str) -> dict[str, Any]:
        video_id = str(UUID(video_id))
        return json.loads(
            self.sql(
                "SELECT row_to_json(s) FROM (SELECT v.status, v.visibility, "
                "v.moderation_status, v.deleted_at IS NOT NULL AS deleted, "
                "v.playback_key, v.storage_bytes, v.reserved_bytes, "
                "j.status AS job_status, j.attempts, j.attempt_token::text "
                "FROM videos v JOIN video_jobs j ON j.video_id=v.id "
                f"WHERE v.id='{video_id}') s;"
            )
        )

    def active_ffmpeg(self, video_id: str) -> bool:
        program = (
            "from pathlib import Path\n"
            "found=False\n"
            "for process in Path('/proc').iterdir():\n"
            " try:\n"
            "  command=(process/'cmdline').read_bytes().split(b'\\0')\n"
            f"  found |= command[0] == b'/usr/bin/ffmpeg' and b'{video_id}' "
            "in b' '.join(command)\n"
            " except (OSError,IndexError): pass\n"
            "print(int(found))\n"
        )
        return self.compose("exec", "-T", "worker", "python", "-c", program) == b"1\n"

    def wait(self, predicate: Any, label: str, timeout: int = 240) -> Any:
        deadline = time.monotonic() + timeout
        next_notice = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (AssertionError, urllib.error.URLError):
                pass
            if time.monotonic() >= next_notice:
                print(f"Waiting: {label}", flush=True)
                next_notice = time.monotonic() + 40
            time.sleep(0.4)
        raise AssertionError(f"Timed out: {label}")

    def ready(self, video_id: str) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            row = self.snapshot(video_id)
            if row["status"] == "FAILED":
                raise RuntimeError("Video failed instead of recovering")
            return row if row["status"] == "READY" else None

        return self.wait(check, "real job recovery and READY")

    def write_report(self) -> None:
        self.args.artifacts.mkdir(parents=True, exist_ok=True)
        (self.args.artifacts / "report.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n"
        )

    def passed(self, name: str, started: float, **evidence: Any) -> None:
        self.report["checks"].append(
            {
                "name": name,
                "status": "passed",
                "seconds": round(time.monotonic() - started, 2),
                **evidence,
            }
        )
        self.write_report()
        print(f"Passed: {name}", flush=True)

    def fixture(self) -> None:
        print("Generating real 45-second 720p source", flush=True)
        with self.args.source.open("wb") as stream:
            result = subprocess.run(
                [
                    *self.command,
                    "run",
                    "--rm",
                    "-T",
                    "--no-deps",
                    "test",
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=1280x720:rate=30:duration=45",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "2",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "32",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "frag_keyframe+empty_moov",
                    "-f",
                    "mp4",
                    "pipe:1",
                ],
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        assert result.returncode == 0, "Real FFmpeg source generation failed"
        assert 0 < self.args.source.stat().st_size < 64 * 1024 * 1024

    def upload(self, owner: Client, viewer_email: str, label: str) -> str:
        video = owner.json(
            "POST",
            "/api/v1/videos",
            expected=201,
            data={
                "title": label,
                "visibility": "private",
                "expected_size": self.args.source.stat().st_size,
            },
        )
        video_id = video["id"]
        owner.request(
            "POST",
            f"/api/v1/videos/{video_id}/grants",
            expected=204,
            data={"email": viewer_email},
        )
        with self.args.source.open("rb") as stream:
            owner.request(
                "PUT",
                f"/api/v1/videos/{video_id}/source",
                expected=202,
                data=stream,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(self.args.source.stat().st_size),
                },
            )
        self.wait(
            lambda: self.active_ffmpeg(video_id),
            f"active FFmpeg before {label}",
            timeout=30,
        )
        return video_id

    def assert_media(self, viewer: Client, guest: Client, video_id: str) -> str:
        playback = f"/media/videos/{video_id}/playback"
        body, _ = viewer.request("GET", playback)
        assert len(body) > 1000
        viewer.request("HEAD", playback)
        segment, headers = viewer.request(
            "GET", playback, headers={"Range": "bytes=0-1023"}, expected=206
        )
        assert segment == body[:1024]
        assert headers["Content-Range"].startswith("bytes 0-")
        for kind in ("playback", "poster"):
            path = f"/media/videos/{video_id}/{kind}"
            viewer.request("HEAD", path)
            guest.request("GET", path, expected=404)
            guest.request("HEAD", path, expected=404)
            guest.request("GET", path, headers={"Range": "bytes=0-15"}, expected=404)
        return hashlib.sha256(body).hexdigest()

    def run(self) -> None:
        self.fixture()
        port = self.compose("port", "nginx", "8080").decode().strip().rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        owner, viewer, guest = Client(base), Client(base), Client(base)
        owner_email = "resilience-owner@example.com"
        viewer_email = "resilience-viewer@example.com"
        owner.register(owner_email)
        viewer.register(viewer_email)
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "migrate",
            "python",
            "-m",
            "app.moderation.bootstrap",
            "--email",
            owner_email,
        )

        started = time.monotonic()
        killed = self.upload(owner, viewer_email, "SIGKILL during FFmpeg")
        first = self.snapshot(killed)
        self.compose("kill", "--signal", "SIGKILL", "worker")
        self.compose("start", "worker")
        recovered = self.ready(killed)
        assert recovered["attempts"] == first["attempts"] + 1
        assert recovered["attempt_token"] != first["attempt_token"]
        assert recovered["job_status"] == "done"
        assert recovered["visibility"] == "private"
        assert recovered["reserved_bytes"] == 0
        self.assert_media(viewer, guest, killed)
        self.assert_one_publication(killed)
        self.passed(
            "SIGKILL worker during FFmpeg; natural lease expiry and recovery",
            started,
            attempts=recovered["attempts"],
            lease_manually_changed=False,
            visibility=recovered["visibility"],
        )

        started = time.monotonic()
        db_lost = self.upload(owner, viewer_email, "Database loss during FFmpeg")
        owner.request(
            "PATCH",
            f"/api/v1/moderation/videos/{db_lost}",
            data={"status": "hidden", "reason": "Проверка сохранения скрытия"},
        )
        assert self.active_ffmpeg(db_lost), "FFmpeg finished before database failure"
        self.compose("stop", "--timeout", "2", "db")
        time.sleep(12)  # Longer than the real five-second processing checkpoint.
        self.compose("start", "db")
        self.wait(
            lambda: self.sql("SELECT 1;") == "1", "PostgreSQL restart", timeout=45
        )
        recovered = self.ready(db_lost)
        assert recovered["attempts"] == 2 and recovered["job_status"] == "done"
        assert recovered["visibility"] == "private"
        assert recovered["moderation_status"] == "hidden"
        for client in (owner, viewer, guest):
            for kind in ("playback", "poster"):
                path = f"/media/videos/{db_lost}/{kind}"
                client.request(
                    "GET", path, headers={"Range": "bytes=0-15"}, expected=404
                )
                client.request("HEAD", path, expected=404)
        owner.request(
            "PATCH",
            f"/api/v1/moderation/videos/{db_lost}",
            data={"status": "visible", "reason": "Проверка завершена"},
        )
        digest = self.assert_media(viewer, guest, db_lost)
        self.assert_one_publication(db_lost)
        self.passed(
            "Database stopped during FFmpeg; recovery preserves private and hidden",
            started,
            attempts=recovered["attempts"],
            hidden_preserved=True,
            database_outage_seconds=12,
        )

        started = time.monotonic()
        deleted = self.upload(owner, viewer_email, "Deletion during FFmpeg")
        owner.request("DELETE", f"/api/v1/videos/{deleted}", expected=204)
        self.wait(
            lambda: not self.active_ffmpeg(deleted),
            "FFmpeg cancellation after deletion",
            timeout=20,
        )
        self.wait(
            lambda: (
                self.snapshot(deleted)["storage_bytes"] == 0
                and self.snapshot(deleted)["reserved_bytes"] == 0
            ),
            "deleted file cleanup and quota release",
            timeout=100,
        )
        row = self.snapshot(deleted)
        assert row["deleted"] and row["playback_key"] is None
        assert row["status"] != "READY" and row["job_status"] != "done"
        for client in (owner, viewer, guest):
            client.request("GET", f"/api/v1/videos/{deleted}", expected=404)
            for kind in ("playback", "poster"):
                path = f"/media/videos/{deleted}/{kind}"
                client.request("HEAD", path, expected=404)
                client.request(
                    "GET", path, headers={"Range": "bytes=0-15"}, expected=404
                )
        self.passed(
            "Deletion during FFmpeg; no publication and quotas released",
            started,
            storage_bytes=row["storage_bytes"],
            reserved_bytes=row["reserved_bytes"],
        )

        started = time.monotonic()
        self.compose("stop", "--timeout", "20", "nginx", "api", "worker", "db")
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "120",
            "db",
            "api",
            "worker",
            "nginx",
        )
        # Docker may allocate a new host port when port 0 is restarted.
        port = self.compose("port", "nginx", "8080").decode().strip().rsplit(":", 1)[1]
        for client in (owner, viewer, guest):
            client.base = f"http://127.0.0.1:{port}"
        assert self.assert_media(viewer, guest, db_lost) == digest
        assert self.snapshot(db_lost)["status"] == "READY"
        self.assert_one_publication(db_lost)
        self.passed(
            "Full stack stop/start preserves sessions, READY, MP4 hash and private ACL",
            started,
            sha256=digest,
        )
        self.report["status"] = "passed"
        self.write_report()

    def assert_one_publication(self, video_id: str) -> None:
        program = (
            "from pathlib import Path; "
            f"root=Path('/var/lib/foxtube/media/videos/{video_id}'); "
            "print(len(list(root.glob('*/playback.mp4'))))"
        )
        # SQL's primary key already enforces one queue row; disk must agree too.
        count = self.compose("exec", "-T", "worker", "python", "-c", program).strip()
        assert count == b"1", "Duplicate or missing published MP4"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    run = Run(parser.parse_args())
    try:
        run.run()
    except Exception as error:
        run.report["status"] = "failed"
        run.report["failure"] = f"{type(error).__name__}: {error}"
        run.write_report()
        print(f"Failed: {type(error).__name__}: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
