from html import escape

import pytest
from fastapi.testclient import TestClient

from app.videos.schemas import VideoPage


@pytest.fixture(autouse=True)
def empty_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rendering tests isolate the template. Integration tests exercise real SQL.
    monkeypatch.setattr(
        "app.web.routes.list_videos",
        lambda *args, **kwargs: VideoPage(items=[], page=1, page_size=20, total=0),
    )


def test_home_and_local_assets(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "FoxTube" in response.text
    assert 'lang="ru"' in response.text
    for asset in ("app.css", "app.js", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_search_escapes_user_text(client: TestClient) -> None:
    query = "<script>alert(1)</script>"
    response = client.get("/search", params={"q": query})
    assert response.status_code == 200
    assert query not in response.text
    assert escape(query) in response.text
    assert client.get("/search", params={"q": "a" * 201}).status_code == 422


def test_local_swagger_and_openapi(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert "cdn.jsdelivr.net" not in response.text
    assert "https://" not in response.text
    assert client.get("/static/docs.js").status_code == 200
    for asset in ("swagger-ui-bundle.js", "swagger-ui.css"):
        assert client.get(f"/static/vendor/swagger-ui/{asset}").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/health/live" in schema["paths"]
    assert "/health/ready" in schema["paths"]


def test_security_headers_and_host_validation(client: TestClient) -> None:
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/", headers={"Host": "attacker.invalid"}).status_code == 400


def test_liveness_independent_of_database_and_readiness_failure(
    client: TestClient,
) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {
            "database": "unavailable",
            "schema": "unavailable",
            "worker": "unavailable",
            "storage": "ok",
        },
    }
    assert "unit-test-secret" not in response.text
    assert "postgresql" not in response.text
