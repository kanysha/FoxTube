from urllib.parse import quote_from_bytes

from starlette.datastructures import URL, Headers
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings


class CanonicalOriginMiddleware:
    """Keep browser pages and their session cookies on the configured origin."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.origin = str(settings.public_origin).rstrip("/")
        self.host = settings.public_origin.host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in {"GET", "HEAD"}:
            headers = Headers(scope=scope)
            host = URL(scope=scope).hostname
            accepts_html = any(
                item.split(";", 1)[0].strip().lower() == "text/html"
                for item in headers.get("accept", "").split(",")
            )
            path = scope["path"]
            service_path = path == "/openapi.json" or any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in ("/api", "/health", "/media", "/static")
            )
            if (
                host in {"localhost", "127.0.0.1"}
                and host != self.host
                and accepts_html
                and not service_path
            ):
                # Never resolve a user-supplied URL: even // paths and URL-like
                # query values remain components of the configured origin.
                raw_path = scope.get("raw_path", path.encode("utf-8"))
                target = (
                    self.origin
                    + "/"
                    + quote_from_bytes(
                        raw_path.removeprefix(b"/"), safe="/%:@!$&'()*+,;=-._~"
                    )
                )
                query = scope.get("query_string", b"")
                if query:
                    target += "?" + quote_from_bytes(query, safe="/%:@!$&'()*+,;=?-._~")
                await RedirectResponse(target, status_code=307)(scope, receive, send)
                return
        await self.app(scope, receive, send)
