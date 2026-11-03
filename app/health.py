from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.readiness import database_checks, storage_available

router = APIRouter(prefix="/health", tags=["Состояние сервисов"])


class Liveness(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/live", response_model=Liveness, summary="Проверить процесс API")
def live() -> Liveness:
    return Liveness(status="ok")


@router.get(
    "/ready",
    response_model=Readiness,
    responses={503: {"model": Readiness}},
    summary="Проверить БД, миграции, worker и хранилище",
)
def ready(request: Request) -> JSONResponse:
    checks = database_checks(request.app.state.engine)
    checks["storage"] = (
        "ok"
        if storage_available(request.app.state.settings.media_root)
        else "unavailable"
    )
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        Readiness(
            status="ok" if healthy else "unavailable", checks=checks
        ).model_dump(),
        status_code=200 if healthy else 503,
    )
