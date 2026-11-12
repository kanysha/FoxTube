from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings


def set_security_headers(
    headers: MutableHeaders, path: str, *, public_https: bool = False
) -> None:
    styles = "'self' 'unsafe-inline'" if path == "/docs" else "'self'"
    headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        f"style-src {styles}; img-src 'self' data:; connect-src 'self'; "
        "media-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers.setdefault("Referrer-Policy", "same-origin")
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    headers.setdefault("Cache-Control", "no-store")
    if public_https:
        # No preload/includeSubDomains: those require control of all subdomains.
        headers["Strict-Transport-Security"] = "max-age=15552000"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        self.app = app
        self.public_https = (
            settings is not None and settings.public_origin.scheme == "https"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                set_security_headers(
                    headers, scope.get("path", ""), public_https=self.public_https
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)
