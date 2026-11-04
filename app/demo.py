"""Opt-in demo data, created through the real HTTP API and video worker."""

import argparse
import fcntl
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from uuid import uuid4

from app.core.config import get_settings

NAMES = (
    "Лиса в кадре",
    "Городские истории",
    "Звук и цвет",
    "Мастерская",
    "Путешествия",
    "Зритель",
)
TOPICS = (
    "Утро: движение и цвет",
    "Вечерний ритм",
    "Приватная зарисовка",
    "Звуковые волны",
)


class DemoError(Exception):
    pass


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        # O_NOFOLLOW and a separate lock also protect an interrupted/repeated seed.
        self.lock = os.open(
            path.with_suffix(".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(self.lock).st_mode):
                raise DemoError("Demo lock must be a regular file.")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.load()
        except BaseException:
            os.close(self.lock)
            raise

    def load(self) -> None:
        path = self.path
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            namespace = uuid4().hex[:12]
            self.data: dict[str, Any] = {
                "version": 1,
                "namespace": namespace,
                "accounts": [
                    {
                        "email": f"fox-demo-{namespace}-{i}@example.com",
                        "password": secrets.token_urlsafe(24),
                        "name": name,
                        "videos": [],
                    }
                    for i, name in enumerate(NAMES)
                ],
            }
            self.save()
        else:
            with os.fdopen(descriptor) as handle:
                mode = os.fstat(handle.fileno()).st_mode
                if not stat.S_ISREG(mode):
                    raise DemoError("Demo manifest must be a regular file.")
                if stat.S_IMODE(mode) & 0o077:
                    raise DemoError("Demo manifest must have permissions 0600.")
                data = json.load(handle)
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or len(data.get("accounts", [])) != 6
            ):
                raise DemoError("Unsupported demo manifest.")
            self.data = data

    def save(self) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".demo-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def close(self) -> None:
        os.close(self.lock)


class InternalCookiePolicy(DefaultCookiePolicy):
    def return_ok_secure(self, cookie: Cookie, request: Request) -> bool:
        # HTTPS deployments issue Secure cookies. The demo runs inside the
        # Compose network and deliberately uses only this fixed internal URL.
        if request.type == "http" and request.host == "nginx:8080":
            return True
        return super().return_ok_secure(cookie, request)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward the explicit public Host/Origin or a request body.
        return None


class Browser:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.host = urlsplit(origin).netloc
        self.opener = build_opener(
            HTTPCookieProcessor(CookieJar(policy=InternalCookiePolicy())), NoRedirect()
        )
        self.csrf = ""
        self.user: dict[str, Any] = {}

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        raw: bytes | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        body = (
            raw
            if raw is not None
            else json.dumps(payload).encode()
            if payload is not None
            else None
        )
        request = Request(
            "http://nginx:8080" + path,
            data=body,
            method=method,
            headers={
                "Host": self.host,
                "Origin": self.origin,
                "X-CSRF-Token": self.csrf,
                "Content-Type": "application/octet-stream"
                if raw is not None
                else "application/json",
            },
        )
        try:
            response = self.opener.open(request, timeout=60)
        except HTTPError as error:
            response = error
        with response:
            if response.status not in expected:
                # Never include HTTP bodies, cookies or submitted credentials.
                raise DemoError(f"{method} {path}: HTTP {response.status}.")
            content = response.read()
            data: dict[str, Any] = json.loads(content) if content else {}
            data["_status"] = response.status
            return data

    def sign_in(self, account: dict[str, Any]) -> None:
        self.csrf = self.call("GET", "/api/v1/auth/session")["csrf_token"]
        payload = {"email": account["email"], "password": account["password"]}
        state = self.call("POST", "/api/v1/auth/login", payload, expected=(200, 401))
        if state["_status"] == 401:
            state = self.call(
                "POST",
                "/api/v1/auth/register",
                {**payload, "display_name": account["name"]},
                expected=(201,),
            )
        self.csrf = state["csrf_token"]
        self.user = state["user"]


def source_video(path: Path, index: int) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={220 + index * 55}:duration=6",
            "-vf",
            f"hue=h={index * 25}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(path),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise DemoError("Could not generate the real demo source with FFmpeg.")
    return path.read_bytes()


def wait_ready(browser: Browser, video_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        video = browser.call("GET", f"/api/v1/videos/{video_id}")
        if video["status"] == "READY":
            return video
        if video["status"] == "FAILED":
            raise DemoError(
                "A demo video failed processing; inspect its studio entry "
                "before retrying."
            )
        time.sleep(1)
    raise DemoError("The worker did not finish a demo video within 180 seconds.")


def seed(manifest: Manifest) -> None:
    origin = str(get_settings().public_origin).rstrip("/")
    browsers: list[Browser] = []
    accounts = manifest.data["accounts"]
    videos: list[dict[str, Any]] = []
    try:
        for account in accounts:
            browser = Browser(origin)
            browser.sign_in(account)
            browsers.append(browser)
        with tempfile.TemporaryDirectory(prefix="foxtube-demo-") as temporary:
            for index in range(12):
                account = accounts[index // 2]
                browser = browsers[index // 2]
                slot = index % 2
                if len(account["videos"]) <= slot:
                    account["videos"].append({})
                record = account["videos"][slot]
                video = (
                    browser.call(
                        "GET", f"/api/v1/videos/{record['id']}", expected=(200, 404)
                    )
                    if record.get("id")
                    else {"_status": 404}
                )
                if video["_status"] == 404 or video.get("status") == "UPLOADING":
                    source = source_video(
                        Path(temporary) / f"source-{index}.mp4", index
                    )
                    if video["_status"] == 404:
                        video = browser.call(
                            "POST",
                            "/api/v1/videos",
                            {
                                "title": f"{TOPICS[index % len(TOPICS)]} · {index + 1}",
                                "description": (
                                    "Демо FoxTube: настоящий шестисекундный ролик "
                                    "со звуком, созданный FFmpeg и обработанный "
                                    "штатным worker."
                                ),
                                "visibility": "private"
                                if index in {3, 9}
                                else "public",
                                "expected_size": len(source),
                            },
                            expected=(201,),
                        )
                        record["id"] = video["id"]
                        manifest.save()
                    browser.call(
                        "PUT",
                        f"/api/v1/videos/{record['id']}/source",
                        raw=source,
                        expected=(202,),
                    )
                videos.append(wait_ready(browser, record["id"]))
                print(f"Demo video {index + 1}/12: READY", flush=True)

        for index, browser in enumerate(browsers):
            own = videos[index * 2]
            target = videos[((index + 1) % 6) * 2]
            browser.call("PUT", f"/api/v1/videos/{target['id']}/like", expected=(204,))
            browser.call(
                "PUT",
                f"/api/v1/channels/{target['channel_id']}/subscription",
                expected=(204,),
            )
            # Keep a deliberately deleted comment deleted, and never infer its
            # absence from the first page of a potentially longer discussion.
            if not accounts[index].get("comment_id"):
                comment = browser.call(
                    "POST",
                    f"/api/v1/videos/{target['id']}/comments",
                    {
                        "body": (
                            f"{accounts[index]['name']}: видео воспроизводится, "
                            "звук и перемотка доступны."
                        )
                    },
                    expected=(201,),
                )
                accounts[index]["comment_id"] = comment["id"]
                manifest.save()
            record = accounts[index]
            playlist = (
                browser.call(
                    "GET",
                    f"/api/v1/playlists/{record['playlist_id']}",
                    expected=(200, 404),
                )
                if record.get("playlist_id")
                else {"_status": 404}
            )
            if playlist["_status"] == 404:
                playlist = browser.call(
                    "POST",
                    "/api/v1/playlists",
                    {
                        "title": f"Подборка: {record['name']}",
                        "description": (
                            "Видео разных авторов. Недоступные видео не видны зрителю."
                        ),
                        "visibility": "public" if index % 2 == 0 else "private",
                    },
                    expected=(201,),
                )
                record["playlist_id"] = playlist["id"]
                manifest.save()
            for video in (own, target):
                browser.call(
                    "PUT",
                    f"/api/v1/playlists/{record['playlist_id']}/items/{video['id']}",
                    expected=(204,),
                )
        for index in (3, 9):
            browsers[index // 2].call(
                "POST",
                f"/api/v1/videos/{videos[index]['id']}/grants",
                {"email": accounts[(index // 2 + 1) % 6]["email"]},
                expected=(204,),
            )
        if not manifest.data.get("report_id"):
            report = browsers[1].call(
                "POST",
                f"/api/v1/videos/{videos[0]['id']}/reports",
                {"reason": "Демонстрационная жалоба для проверки очереди модерации."},
                expected=(201,),
            )
            manifest.data["report_id"] = report["id"]
            manifest.save()
        print(
            "Demo ready: 6 accounts, 12 processed videos, 6 comments, "
            "6 playlists, subscriptions and a report."
        )
    finally:
        for browser in browsers:
            try:
                browser.call("POST", "/api/v1/auth/logout", expected=(200,))
            except (DemoError, URLError, OSError):
                # Cleanup must not replace the useful failure from the seed.
                print("Demo session cleanup could not reach the API.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create persistent demo data through the actual API."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest: Manifest | None = None
    try:
        manifest = Manifest(args.manifest)
        seed(manifest)
    except (DemoError, OSError, URLError, ValueError, KeyError, TypeError) as error:
        # Credentials and external response bodies must never enter error logs.
        print(
            str(error)
            if isinstance(error, DemoError)
            else "Demo failed; check configuration, manifest and service health."
        )
        return 1
    finally:
        if manifest is not None:
            manifest.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
