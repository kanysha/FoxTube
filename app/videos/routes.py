from typing import Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.auth.dependencies import (
    client_ip,
    mutation_session,
    optional_session,
    required_user,
)
from app.auth.rate_limits import check_limits
from app.auth.schemas import UserView
from app.auth.service import SessionContext
from app.core.text import DatabaseText
from app.videos import service
from app.videos.schemas import (
    GrantInput,
    GrantPage,
    VideoInput,
    VideoPage,
    VideoPatch,
    VideoRecommendations,
    VideoView,
)
from app.videos.storage import media_path
from app.videos.upload import UploadSink

router = APIRouter(prefix="/api/v1/videos", tags=["Видео"])
media_router = APIRouter(prefix="/media/videos", tags=["Защищённые медиа"])


def mutation_user(
    state: Annotated[SessionContext, Depends(mutation_session)],
) -> UserView:
    if state.user is None:
        raise HTTPException(401, "Необходим вход.")
    return state.user


Mutator = Annotated[UserView, Depends(mutation_user)]
Reader = Annotated[SessionContext | None, Depends(optional_session)]


@router.get("", response_model=VideoPage)
def feed(
    request: Request,
    state: Reader,
    q: Annotated[DatabaseText, Query(max_length=200)] = "",
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> VideoPage:
    return service.list_videos(
        request.app.state.engine,
        state.user.id if state and state.user else None,
        q.strip(),
        page,
        page_size=page_size,
    )


@router.get("/mine", response_model=VideoPage)
def mine(
    request: Request,
    user: Annotated[UserView, Depends(required_user)],
    page: int = Query(default=1, ge=1, le=10000),
) -> VideoPage:
    return service.list_videos(request.app.state.engine, user.id, page=page, mine=True)


@router.post("", response_model=VideoView, status_code=201)
def create(request: Request, data: VideoInput, user: Mutator) -> VideoView:
    """Создать резерв на файл + результат (5 ГиБ на аккаунт, очередь до 20).

    Затем передайте файл через PUT /{id}/source в течение пяти минут.
    Одна незавершённая загрузка на аккаунт; передача ограничена 30 минутами.
    """
    check_limits(
        request.app.state.engine,
        [("video-user", str(user.id), 10), ("video-ip", client_ip(request), 20)],
    )
    return service.create_video(
        request.app.state.engine, request.app.state.settings.media_root, user.id, data
    )


@router.get("/{video_id}", response_model=VideoView)
def detail(request: Request, video_id: UUID, state: Reader) -> VideoView:
    return service.get_video(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
    )


@router.patch("/{video_id}", response_model=VideoView)
def update(
    request: Request, video_id: UUID, data: VideoPatch, user: Mutator
) -> VideoView:
    return service.patch_video(request.app.state.engine, video_id, user.id, data)


@router.get("/{video_id}/recommendations", response_model=VideoRecommendations)
def recommendations(
    request: Request,
    video_id: UUID,
    state: Reader,
    limit: int = Query(default=12, ge=1, le=20),
) -> VideoRecommendations:
    return service.recommendations(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
        limit,
    )


@router.delete("/{video_id}", status_code=204)
def delete(request: Request, video_id: UUID, user: Mutator) -> Response:
    service.delete_video(request.app.state.engine, video_id, user.id)
    return Response(status_code=204)


@router.put(
    "/{video_id}/source",
    status_code=202,
    response_model=VideoView,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def source(request: Request, video_id: UUID, user: Mutator) -> VideoView:
    """Передать MP4/MOV/WebM до 500 МиБ без multipart.

    Авторизация, CSRF, размер и резерв проверяются до чтения тела. Повтор принятой
    передачи не создаёт задания. Прерванную передачу повторяют целиком.
    """
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/octet-stream"
    ):
        raise HTTPException(415, "Передайте файл как application/octet-stream.")
    length_text = request.headers.get("content-length")
    if length_text is not None and (
        not length_text.isascii() or not length_text.isdigit() or len(length_text) > 10
    ):
        raise HTTPException(400, "Некорректный размер запроса.")
    length = int(length_text) if length_text is not None else None
    engine = request.app.state.engine
    ticket = await run_in_threadpool(
        service.begin_upload,
        engine,
        request.app.state.settings.media_root,
        video_id,
        user.id,
        length,
    )
    if ticket is not None:
        sink = UploadSink(engine, request.app.state.settings.media_root, ticket)
        try:
            await run_in_threadpool(sink.open)
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(sink.write, chunk)
            await run_in_threadpool(sink.publish)
        except ClientDisconnect as error:
            raise HTTPException(
                400, "Передача прервана. Повторите загрузку."
            ) from error
        except OSError as error:
            raise HTTPException(
                507, "Не удалось сохранить файл. Повторите позже."
            ) from error
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await run_in_threadpool(sink.close)
                except SQLAlchemyError:
                    pass  # An expired lease and orphan sweep reconcile DB outages.
    return await run_in_threadpool(service.get_video, engine, video_id, user.id)


@router.get("/{video_id}/grants", response_model=GrantPage)
def grants(
    request: Request, video_id: UUID, user: Annotated[UserView, Depends(required_user)]
) -> GrantPage:
    return service.list_grants(request.app.state.engine, video_id, user.id)


@router.post("/{video_id}/grants", status_code=204)
def grant(
    request: Request, video_id: UUID, data: GrantInput, user: Mutator
) -> Response:
    service.add_grant(request.app.state.engine, video_id, user.id, str(data.email))
    return Response(status_code=204)


@router.delete("/{video_id}/grants/{recipient}", status_code=204)
def revoke(
    request: Request, video_id: UUID, recipient: UUID, user: Mutator
) -> Response:
    service.revoke_grant(request.app.state.engine, video_id, user.id, recipient)
    return Response(status_code=204)


@media_router.get("/{video_id}/{kind}", operation_id="getVideoMedia")
@media_router.head("/{video_id}/{kind}", operation_id="headVideoMedia")
def media(request: Request, video_id: UUID, kind: str, state: Reader) -> Response:
    if kind not in {"playback", "poster"}:
        raise HTTPException(404, "Видео недоступно.")
    key = service.media_key(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
        kind == "poster",
    )
    path = media_path(request.app.state.settings.media_root, key)
    if not path.is_file():
        raise HTTPException(404, "Видео недоступно.")
    return Response(
        headers={
            "X-Accel-Redirect": f"/internal-media/{key}",
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
        },
        media_type="image/jpeg" if kind == "poster" else "video/mp4",
    )
