from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.request_limits import AuthBodyLimitMiddleware


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/playlists",
        "/api/v1/comments/comment-id",
        "/api/v1/admin/roles",
        "/api/v1/channels/anything/source",
        "/api/v1/videos/invalid-id/source",
    ],
)
@pytest.mark.parametrize("chunked", [False, True])
def test_new_api_and_source_lookalikes_have_bounded_bodies(
    path: str, chunked: bool
) -> None:
    app = FastAPI()
    app.add_middleware(AuthBodyLimitMiddleware)
    called = False

    @app.put("/{path:path}")
    async def downstream(request: Request, path: str) -> dict[str, int]:
        nonlocal called
        called = True
        return {"bytes": len(await request.body())}

    def chunks() -> Iterator[bytes]:
        yield b"a" * 8192
        yield b"b" * 8193

    with TestClient(app) as client:
        response = client.put(path, content=chunks() if chunked else b"a" * 16385)
    assert response.status_code == 413
    assert not called


def test_binary_upload_still_streams_beyond_metadata_limit() -> None:
    app = FastAPI()
    app.add_middleware(AuthBodyLimitMiddleware)

    @app.put("/api/v1/videos/{video_id}/source")
    async def source(request: Request, video_id: str) -> dict[str, int]:
        return {"bytes": sum([len(chunk) async for chunk in request.stream()])}

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/videos/14e69704-06b6-4286-83be-77325c71801e/source",
            content=b"a" * 16385,
        )
    assert response.status_code == 200
    assert response.json() == {"bytes": 16385}
