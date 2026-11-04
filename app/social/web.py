from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.dependencies import optional_session
from app.social import service
from app.web.routes import templates, viewer_context

router = APIRouter(include_in_schema=False)


@router.get("/playlists", response_class=HTMLResponse, response_model=None)
def playlists(
    request: Request, page: Annotated[int, Query(ge=1, le=10000)] = 1
) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    result = service.playlists(
        request.app.state.engine, state.user.id, state.user.id, page
    )
    return templates.TemplateResponse(
        request=request,
        name="playlists.html",
        context={
            "page_title": "Мои плейлисты",
            "active_page": "playlists",
            "query": "",
            "playlists": result.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist(
    request: Request,
    playlist_id: UUID,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> HTMLResponse:
    state = optional_session(request)
    result = service.playlist_detail(
        request.app.state.engine,
        playlist_id,
        state.user.id if state and state.user else None,
        page,
    )
    return templates.TemplateResponse(
        request=request,
        name="playlist.html",
        context={
            "page_title": result.playlist.title,
            "active_page": "playlists",
            "query": "",
            **result.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/subscriptions", response_class=HTMLResponse, response_model=None)
def subscriptions(
    request: Request,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    channels_page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    videos = service.subscriptions_feed(request.app.state.engine, state.user.id, page)
    channels = service.subscribed_channels(
        request.app.state.engine, state.user.id, channels_page
    )
    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "page_title": "Подписки",
            "active_page": "subscriptions",
            "query": "",
            "videos": videos.model_dump(mode="json"),
            "channels": channels.model_dump(mode="json"),
            **viewer_context(state),
        },
    )
