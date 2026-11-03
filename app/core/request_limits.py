from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUTH_BODY_LIMIT = 16 * 1024


class AuthBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not scope["path"].startswith(("/api/v1/auth/", "/api/v1/videos"))
            or scope["path"].endswith("/source")
            or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}
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
