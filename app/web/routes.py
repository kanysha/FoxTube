from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import ensure_session, optional_session, set_session_cookie
from app.auth.service import SessionContext
from app.channels.service import get_channel
from app.core.text import DatabaseText
from app.social.service import channel_playlists
from app.videos.service import get_video, list_videos

WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
router = APIRouter(include_in_schema=False)


def viewer_context(state: SessionContext | None) -> dict[str, object]:
    return {
        "viewer": state.user.model_dump(mode="json") if state and state.user else None,
        "csrf_token": state.csrf_token if state else None,
    }


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request, page: Annotated[int, Query(ge=1, le=10000)] = 1
) -> HTMLResponse:
    state = optional_session(request)
    videos = list_videos(
        request.app.state.engine,
        state.user.id if state and state.user else None,
        page=page,
    )
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "page_title": "Главная",
            "active_page": "home",
            "query": "",
            "videos": videos.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: Annotated[DatabaseText, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> HTMLResponse:
    state = optional_session(request)
    videos = (
        list_videos(
            request.app.state.engine,
            state.user.id if state and state.user else None,
            query=q.strip(),
            page=page,
        )
        if q.strip()
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "page_title": "Поиск",
            "active_page": "search",
            "query": q.strip(),
            "videos": videos.model_dump(mode="json") if videos else None,
            **viewer_context(state),
        },
    )


@router.get("/docs", response_class=HTMLResponse)
def docs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="docs.html")


def auth_page(
    request: Request, name: str, title: str
) -> HTMLResponse | RedirectResponse:
    state = ensure_session(request)
    if state.user:
        return RedirectResponse("/account", status_code=303)
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "page_title": title,
            "active_page": "",
            "query": "",
            **viewer_context(state),
        },
    )
    if state.issued:
        set_session_cookie(response, state, request.app.state.settings)
    return response


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login(request: Request) -> HTMLResponse | RedirectResponse:
    return auth_page(request, "login.html", "Вход")


@router.get("/register", response_class=HTMLResponse, response_model=None)
def register(request: Request) -> HTMLResponse | RedirectResponse:
    return auth_page(request, "register.html", "Регистрация")


@router.get("/account", response_class=HTMLResponse, response_model=None)
def account(request: Request) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "page_title": "Мой аккаунт",
            "active_page": "account",
            "query": "",
            **viewer_context(state),
        },
    )


@router.get("/upload", response_class=HTMLResponse, response_model=None)
def upload(request: Request) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "page_title": "Загрузить видео",
            "active_page": "upload",
            "query": "",
            **viewer_context(state),
        },
    )


@router.get("/studio", response_class=HTMLResponse, response_model=None)
def studio(
    request: Request, page: Annotated[int, Query(ge=1, le=10000)] = 1
) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    videos = list_videos(request.app.state.engine, state.user.id, page=page, mine=True)
    return templates.TemplateResponse(
        request=request,
        name="studio.html",
        context={
            "page_title": "Мои видео",
            "active_page": "studio",
            "query": "",
            "videos": videos.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/watch/{video_id}", response_class=HTMLResponse)
def watch(request: Request, video_id: UUID) -> HTMLResponse:
    state = optional_session(request)
    video = get_video(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
    )
    return templates.TemplateResponse(
        request=request,
        name="watch.html",
        context={
            "page_title": video.title,
            "active_page": "",
            "query": "",
            "video": video.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel(
    request: Request,
    channel_id: UUID,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    playlist_page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> HTMLResponse:
    state = optional_session(request)
    viewer_id = state.user.id if state and state.user else None
    info = get_channel(request.app.state.engine, viewer_id, channel_id=channel_id)
    videos = list_videos(
        request.app.state.engine,
        viewer_id,
        page=page,
        channel_id=channel_id,
    )
    playlists = channel_playlists(
        request.app.state.engine, channel_id, viewer_id, page=playlist_page
    )
    return templates.TemplateResponse(
        request=request,
        name="channel.html",
        context={
            "page_title": info.title,
            "active_page": "channel" if info.can_manage else "",
            "query": "",
            "channel": info.model_dump(mode="json"),
            "videos": videos.model_dump(mode="json"),
            "playlists": playlists.model_dump(mode="json"),
            **viewer_context(state),
        },
    )


@router.get("/@{handle}")
def channel_handle(
    request: Request,
    handle: Annotated[DatabaseText, PathParameter(min_length=1, max_length=80)],
) -> RedirectResponse:
    info = get_channel(request.app.state.engine, None, handle=handle)
    return RedirectResponse(f"/channels/{info.id}", status_code=303)
