from uuid import UUID

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUTH_BODY_LIMIT = 16 * 1024


def is_video_source(scope: Scope) -> bool:
    if scope["method"] != "PUT":
        return False
    parts = str(scope["path"]).split("/")
    if len(parts) != 6 or parts[1:4] != ["api", "v1", "videos"] or parts[5] != "source":
        return False
    try:
        return str(UUID(parts[4])) == parts[4].lower()
    except ValueError:
        return False


class AuthBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/api/v1/")
            or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}
            or is_video_source(scope)
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        length = headers.get(b"content-length", b"")
        if length.isdigit() and (len(length) > 8 or int(length) > AUTH_BODY_LIMIT):
            await JSONResponse({"detail": "Слишком большой запрос."}, 413)(
                scope, receive, send
            )
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > AUTH_BODY_LIMIT:
                response = JSONResponse({"detail": "Слишком большой запрос."}, 413)
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
