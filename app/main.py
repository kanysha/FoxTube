from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from app.auth.routes import router as auth_router
from app.channels.routes import router as channel_router
from app.core.config import Settings, get_settings
from app.core.db import make_engine
from app.core.request_limits import AuthBodyLimitMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.health import router as health_router
from app.moderation.routes import router as moderation_router
from app.moderation.web import router as moderation_web_router
from app.social.routes import router as social_router
from app.social.web import router as social_web_router
from app.videos.routes import media_router
from app.videos.routes import router as video_router
from app.web.routes import WEB_ROOT
from app.web.routes import router as web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title="FoxTube API",
        version="0.1.0",
        description=(
            "API FoxTube: учётные записи, каналы, видео, подписки, "
            "комментарии, плейлисты и модерация."
        ),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.engine = engine
    application.add_middleware(AuthBodyLimitMiddleware)
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts
    )
    application.add_middleware(SecurityHeadersMiddleware)

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's input/context can contain the submitted password.
        details = [
            {key: item[key] for key in ("loc", "msg", "type")}
            for item in error.errors()
        ]
        return JSONResponse({"detail": details}, status_code=422)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Сервис временно недоступен. Повторите позже."},
            status_code=503,
        )

    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(channel_router)
    application.include_router(video_router)
    application.include_router(social_router)
    application.include_router(media_router)
    application.include_router(moderation_router)
    application.include_router(moderation_web_router)
    application.include_router(web_router)
    application.include_router(social_web_router)
    application.mount(
        "/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static"
    )
    return application
