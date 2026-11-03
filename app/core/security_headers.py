from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                styles = (
                    "'self' 'unsafe-inline'"
                    if scope.get("path") == "/docs"
                    else "'self'"
                )
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; "
                    f"style-src {styles}; img-src 'self' data:; connect-src 'self'; "
                    "object-src 'none'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'"
                )
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)
