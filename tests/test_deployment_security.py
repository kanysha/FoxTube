from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app


def test_production_rejects_plain_http_origin() -> None:
    with pytest.raises(ValidationError, match="production requires an HTTPS"):
        Settings(
            env="production",
            public_origin="http://foxtube.example",
            db_password=SecretStr("test-only"),
        )


def test_https_origin_enables_hsts_without_trusting_forwarding_headers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        env="production",
        public_origin="https://foxtube.example",
        db_password=SecretStr("test-only"),
        db_host="127.0.0.1",
        db_port=1,
        media_root=tmp_path,
    )
    with TestClient(create_app(settings), base_url="https://foxtube.example") as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"] == "max-age=15552000"
        assert "includeSubDomains" not in response.headers["strict-transport-security"]
        assert response.headers["cache-control"] == "no-store"
        response = client.get(
            "/health/live",
            headers={
                "Host": "attacker.invalid",
                "Forwarded": "host=foxtube.example;proto=https",
                "X-Forwarded-Host": "foxtube.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 400


def test_http_does_not_enable_hsts_from_attacker_headers(client: TestClient) -> None:
    response = client.get(
        "/health/live",
        headers={"X-Forwarded-Proto": "https", "Forwarded": "proto=https"},
    )
    assert response.status_code == 200
    assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/health/live/"), ("GET", "/static"), ("POST", "/api/v1/auth/login/")],
)
def test_tls_proxy_redirects_keep_https(method: str, path: str) -> None:
    settings = Settings(
        env="production",
        public_origin="https://foxtube.example",
        db_password=SecretStr("test-only"),
    )
    with TestClient(create_app(settings), base_url="http://foxtube.example") as client:
        response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://foxtube.example/")


def test_http_redirect_ignores_spoofed_forwarding_scheme(client: TestClient) -> None:
    response = client.get(
        "/health/live/",
        follow_redirects=False,
        headers={"X-Forwarded-Proto": "https", "Forwarded": "proto=https"},
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("http://")


def test_public_assets_cache_and_revalidate_without_a_session(
    client: TestClient,
) -> None:
    response = client.get("/static/app.css")
    assert response.status_code == 200
    cache = "public, max-age=300, must-revalidate"
    assert response.headers["cache-control"] == cache
    assert "set-cookie" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    etag = response.headers["etag"]
    last_modified = response.headers["last-modified"]
    for headers in ({"If-None-Match": etag}, {"If-Modified-Since": last_modified}):
        unchanged = client.get("/static/app.css", headers=headers)
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["cache-control"] == cache
        assert "set-cookie" not in unchanged.headers
    head = client.head("/static/app.css")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["cache-control"] == cache
    partial = client.get("/static/app.css", headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == response.content[:10]
    assert partial.headers["cache-control"] == cache


@pytest.mark.parametrize(
    "path",
    [
        "/static/missing.css",
        "/static/%2e%2e/%2e%2e/.env",
        "/api/v1/missing",
        "/media/missing",
        "/internal-media/missing.mp4",
        "/missing-page",
    ],
)
def test_errors_and_private_paths_never_receive_public_cache(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers={"Range": "bytes=0-9"})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "x-accel-redirect" not in response.headers
