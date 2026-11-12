"""Public code assets may be cached; user media never uses this mount."""

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class PublicStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in {200, 206, 304}:
            # Assets have unversioned URLs. Revalidate after a short freshness
            # window so a deployment cannot strand clients on old JS for a year.
            response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        return response
