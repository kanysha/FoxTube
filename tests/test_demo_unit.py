import os
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.request import Request

import pytest

from app.demo import DemoError, InternalCookiePolicy, Manifest


def test_demo_manifest_is_private_and_preserved_on_retry(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    first = Manifest(path)
    try:
        original = path.read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
        assert len(first.data["accounts"]) == 6
        assert len({item["password"] for item in first.data["accounts"]}) == 6
        with pytest.raises(BlockingIOError):
            Manifest(path)
    finally:
        first.close()
    second = Manifest(path)
    try:
        if original != path.read_bytes():
            pytest.fail("A repeated seed replaced its persistent credentials")
    finally:
        second.close()


def test_demo_rejects_public_manifest_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = Manifest(path)
    manifest.close()
    os.chmod(path, 0o644)
    with pytest.raises(DemoError, match="0600"):
        Manifest(path)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        Manifest(link)


@pytest.mark.parametrize("filename", ["manifest.json", "manifest.lock"])
def test_demo_rejects_fifo_without_waiting_for_writer(
    tmp_path: Path, filename: str
) -> None:
    os.mkfifo(tmp_path / filename, 0o600)
    with pytest.raises(DemoError, match="regular file"):
        Manifest(tmp_path / "manifest.json")


@pytest.mark.parametrize(
    ("url", "sent"),
    [
        ("http://nginx:8080/api/v1/auth/me", True),
        ("http://nginx:8081/api/v1/auth/me", False),
        ("http://nginx/api/v1/auth/me", False),
        ("http://external.example/api/v1/auth/me", False),
        ("https://external.example/api/v1/auth/me", False),
    ],
)
def test_secure_demo_cookie_is_limited_to_internal_endpoint(
    url: str, sent: bool
) -> None:
    jar = CookieJar(policy=InternalCookiePolicy())
    jar.set_cookie(
        Cookie(
            version=0,
            name="session",
            value="disposable-cookie",
            port=None,
            port_specified=False,
            domain="nginx.local",
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )
    )
    request = Request(url)
    jar.add_cookie_header(request)
    assert request.has_header("Cookie") is sent
