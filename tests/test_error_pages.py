from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError


@pytest.mark.parametrize(
    ("path", "status", "title"),
    [
        ("/no-such-page", 404, "Страница недоступна"),
        ("/watch/not-a-uuid", 422, "Проверьте адрес"),
        ("/channels/not-a-uuid", 422, "Проверьте адрес"),
        ("/playlists/not-a-uuid", 422, "Проверьте адрес"),
        ("/search?page=0", 422, "Проверьте адрес"),
        ("/search?q=" + "x" * 201, 422, "Проверьте адрес"),
    ],
)
def test_invalid_browser_addresses_have_recovery_links(
    client: TestClient, path: str, status: int, title: str
) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert f'<h1 id="page-title">{title}</h1>' in response.text
    assert 'class="site-header"' in response.text
    assert 'href="/">На главную</a>' in response.text
    assert 'href="/search">Найти видео</a>' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_unavailable_video_never_exposes_the_service_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def inaccessible(*args: object, **kwargs: object) -> None:
        raise HTTPException(404, "private-title internal-file secret@example.com")

    monkeypatch.setattr("app.web.routes.get_video", inaccessible)
    missing = client.get("/missing")
    denied = client.get(f"/watch/{uuid4()}")
    assert denied.status_code == 404
    assert denied.text == missing.text
    assert "private-title" not in denied.text


@pytest.mark.parametrize("status", [401, 403, 429])
def test_page_http_errors_preserve_status_and_headers(
    client: TestClient, status: int
) -> None:
    def failure() -> None:
        raise HTTPException(status, "internal-detail", headers={"Retry-After": "60"})

    client.app.add_api_route("/failure", failure)
    response = client.get("/failure")
    assert response.status_code == status
    assert response.headers["retry-after"] == "60"
    assert "internal-detail" not in response.text
    if status == 401:
        assert 'href="/login">Войти</a>' in response.text


def test_wrong_page_method_preserves_allow(client: TestClient) -> None:
    response = client.post("/search")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert 'data-error-status="405"' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/v1/missing",
        "/media",
        "/media/missing",
        "/health/missing",
        "/static/missing.css",
        "/internal-media/missing",
    ],
)
def test_service_not_found_stays_json_even_for_browser_accept(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path", ["/api/v1/videos/not-a-uuid", "/media/videos/not-a-uuid/playback"]
)
def test_service_validation_stays_sanitized_json(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()["detail"][0]) == {"loc", "msg", "type"}


@pytest.mark.parametrize("method", ["HEAD", "GET"])
def test_denied_media_range_is_never_an_html_page(
    client: TestClient, method: str
) -> None:
    response = client.request(
        method,
        f"/media/videos/{uuid4()}/playback",
        headers={"Accept": "text/html", "Range": "bytes=0-63"},
    )
    # The database in this unit fixture is intentionally unavailable.
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert "x-accel-redirect" not in response.headers
    if method == "HEAD":
        assert response.content == b""


@pytest.mark.parametrize("path", ["/failure", "/api/v1/failure"])
@pytest.mark.parametrize("exception", [SQLAlchemyError, RuntimeError])
def test_server_errors_are_safe_even_when_session_storage_is_broken(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    exception: type[Exception],
) -> None:
    def failure() -> None:
        raise exception("postgresql://user:secret@database/private-file")

    def forbidden_session_lookup(*args: object) -> None:
        pytest.fail("Error response tried to reconnect to session storage")

    monkeypatch.setattr("app.web.errors.optional_session", forbidden_session_lookup)
    client.app.add_api_route(path, failure)
    with TestClient(
        client.app, base_url="http://localhost", raise_server_exceptions=False
    ) as browser:
        response = browser.get(path, headers={"Accept": "text/html"})
    assert response.status_code == (503 if exception is SQLAlchemyError else 500)
    assert "secret" not in response.text
    assert "postgresql" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    if path.startswith("/api/"):
        assert isinstance(response.json()["detail"], str)
    else:
        assert 'href="/">На главную</a>' in response.text


def test_error_page_survives_stale_session_and_escapes_untrusted_address(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_storage(*args: object) -> None:
        raise SQLAlchemyError("secret")

    monkeypatch.setattr("app.web.errors.optional_session", broken_storage)
    response = client.get('/missing?<script>alert("secret")</script>')
    assert response.status_code == 404
    assert "secret" not in response.text
    assert "<script>alert" not in response.text
    assert "set-cookie" not in response.headers


def test_page_database_failure_has_html_recovery(client: TestClient) -> None:
    response = client.get(f"/watch/{uuid4()}")
    assert response.status_code == 503
    assert 'data-error-status="503"' in response.text
