from datetime import UTC, datetime
from html import escape
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.channels.schemas import ChannelView
from app.social.schemas import PlaylistPage
from app.videos.schemas import VideoPage, VideoView


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


def test_channel_handle_rejects_invalid_text_before_database_access(
    client: TestClient,
) -> None:
    assert client.get("/@bad%00handle").status_code == 422
    assert client.get("/@" + "a" * 81).status_code == 422


def test_channel_video_pagination_preserves_playlist_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel_id, owner_id, video_id = uuid4(), uuid4(), uuid4()
    created_at = datetime.now(UTC)
    channel = ChannelView(
        id=channel_id,
        handle="pagination-channel",
        title="Канал с несколькими страницами",
        created_at=created_at,
        can_manage=False,
        videos_count=60,
    )
    video = VideoView(
        id=video_id,
        title="Видео второй страницы",
        description="",
        visibility="public",
        status="READY",
        error=None,
        duration=10,
        width=320,
        height=180,
        created_at=created_at,
        owner_id=owner_id,
        channel_id=channel_id,
        channel_name=channel.title,
        can_manage=False,
        playback_url=f"/media/videos/{video_id}/playback",
        poster_url=f"/media/videos/{video_id}/poster",
    )
    monkeypatch.setattr("app.web.routes.get_channel", lambda *a, **kw: channel)
    monkeypatch.setattr(
        "app.web.routes.list_videos",
        lambda *a, **kw: VideoPage(
            items=[video], page=kw["page"], page_size=20, total=60
        ),
    )
    monkeypatch.setattr(
        "app.web.routes.channel_playlists",
        lambda *a, **kw: PlaylistPage(
            items=[], page=kw["page"], page_size=20, total=60
        ),
    )

    response = client.get(f"/channels/{channel_id}?page=2&playlist_page=2")

    assert response.status_code == 200
    for target_page in (1, 3):
        assert (
            f'href="/channels/{channel_id}?page={target_page}&amp;playlist_page=2"'
            in response.text
        )


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
    assert "media-src 'self' blob:" in response.headers["content-security-policy"]
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
