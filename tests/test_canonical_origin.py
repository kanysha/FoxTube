from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth.dependencies import mutation_session
from app.auth.service import SessionContext, new_session
from app.core.config import Settings
from app.main import create_app


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("page", ["/", "/login", "/register", "/account", "/docs"])
def test_loopback_browser_navigation_uses_canonical_origin(
    client: TestClient, method: str, page: str
) -> None:
    response = client.request(
        method,
        f"http://127.0.0.1:8080{page}?next=https%3A%2F%2Fattacker.invalid%2F&q=a%26b",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == (
        f"http://localhost:8080{page}?next=https%3A%2F%2Fattacker.invalid%2F&q=a%26b"
    )
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_alias_redirect_precedes_session_creation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    visited_hosts: list[str | None] = []

    def ensure_session(request: Request) -> SessionContext:
        visited_hosts.append(request.url.hostname)
        return new_session(None)

    monkeypatch.setattr("app.web.routes.ensure_session", ensure_session)
    response = client.get(
        "http://127.0.0.1:8080/login", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    assert str(response.url) == "http://localhost:8080/login"
    assert visited_hosts == ["localhost"]
    assert 'name="csrf-token"' in response.text
    assert response.cookies.get("foxtube_session")


@pytest.mark.parametrize(
    "path",
    [
        "/health/live",
        "/openapi.json",
        "/static/auth.js",
        "/api/missing",
        "/media/missing",
    ],
)
def test_alias_service_requests_do_not_redirect(client: TestClient, path: str) -> None:
    response = client.get(
        "http://127.0.0.1:8080" + path,
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code in {200, 404}
    assert "location" not in response.headers


def test_alias_json_and_mutation_requests_do_not_redirect(client: TestClient) -> None:
    response = client.get(
        "http://127.0.0.1:8080/docs", headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    response = client.post(
        "http://127.0.0.1:8080/login", headers={"Accept": "text/html"}
    )
    assert response.status_code == 405


def test_encoded_path_cannot_replace_canonical_authority(client: TestClient) -> None:
    response = client.get(
        "http://127.0.0.1:8080/%2F%2Fattacker.invalid/%23fragment",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == (
        "http://localhost:8080/%2F%2Fattacker.invalid/%23fragment"
    )


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8080", "https://192.168.1.16:8080"]
)
def test_configured_origin_is_the_only_redirect_destination(
    tmp_path: Path, origin: str
) -> None:
    settings = Settings(
        public_origin=origin,
        db_password=SecretStr("disposable-unit-test-secret"),
        media_root=tmp_path,
    )
    with TestClient(create_app(settings), base_url="http://localhost:8080") as browser:
        response = browser.get(
            "/search?q=%2F%2Fattacker.invalid%2F%23fragment",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 307
        assert response.headers["location"] == (
            origin + "/search?q=%2F%2Fattacker.invalid%2F%23fragment"
        )
        response = browser.get(
            "/login",
            headers={"Host": "attacker.invalid", "Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "location" not in response.headers


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8080", "http://localhost", "https://localhost:8080", "null"],
)
def test_alias_redirect_does_not_relax_mutation_origin(
    client: TestClient, origin: str
) -> None:
    state = new_session(None)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "app": client.app,
            "headers": [
                (b"origin", origin.encode()),
                (b"x-csrf-token", state.csrf_token.encode()),
            ],
        }
    )
    with pytest.raises(HTTPException) as rejected:
        mutation_session(request, state)
    assert rejected.value.status_code == 403
