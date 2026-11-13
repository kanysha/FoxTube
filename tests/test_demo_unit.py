import os
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.request import Request

import pytest

from app.demo import Browser, DemoError, InternalCookiePolicy, Manifest


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
    ("url", "domain", "sent"),
    [
        ("http://nginx:8080/api/v1/auth/me", "nginx.local", True),
        ("http://nginx:8081/api/v1/auth/me", "nginx.local", False),
        ("http://nginx/api/v1/auth/me", "nginx.local", False),
        ("http://api:8000/api/v1/auth/me", "api.local", True),
        ("http://api:8001/api/v1/auth/me", "api.local", False),
        ("http://api/api/v1/auth/me", "api.local", False),
        ("http://external.example/api/v1/auth/me", "external.example", False),
        ("http://external.example/api/v1/auth/me", "api.local", False),
        ("https://external.example/api/v1/auth/me", "api.local", False),
    ],
)
def test_secure_demo_cookie_is_limited_to_internal_endpoint(
    url: str, domain: str, sent: bool
) -> None:
    jar = CookieJar(policy=InternalCookiePolicy())
    jar.set_cookie(
        Cookie(
            version=0,
            name="session",
            value="disposable-cookie",
            port=None,
            port_specified=False,
            domain=domain,
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


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_demo_transport_retains_public_origin_and_secure_session(scheme: str) -> None:
    from email.message import Message
    from io import BytesIO
    from urllib.request import BaseHandler
    from urllib.response import addinfourl

    class Transport(BaseHandler):
        # Intercept only the transport; use the real opener and CookieJar.
        handler_order = 400

        def http_open(self, request: Request) -> addinfourl:
            assert request.host == ("api:8000" if scheme == "https" else "nginx:8080")
            assert request.get_header("Host") == "demo.example"
            assert request.get_header("Origin") == f"{scheme}://demo.example"
            headers = Message()
            if request.get_method() == "GET":
                headers["Set-Cookie"] = "session=synthetic; Secure; HttpOnly; Path=/"
            else:
                assert request.get_header("Cookie") == "session=synthetic"
                assert request.get_header("X-csrf-token") == "synthetic-csrf"
            result = addinfourl(BytesIO(b"{}"), headers, request.full_url, 200)
            result.msg = "OK"
            return result

    browser = Browser(f"{scheme}://demo.example")
    browser.opener.add_handler(Transport())
    browser.call("GET", "/api/v1/auth/session")
    browser.csrf = "synthetic-csrf"
    browser.call("POST", "/api/v1/auth/logout", {})
