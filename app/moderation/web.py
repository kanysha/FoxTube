from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.dependencies import optional_session
from app.moderation.service import list_audit, list_reports
from app.web.routes import templates, viewer_context

router = APIRouter(include_in_schema=False)


@router.get("/moderation", response_class=HTMLResponse, response_model=None)
def moderation(
    request: Request,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    closed: bool = False,
) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    reports = list_reports(
        request.app.state.engine,
        state.user.id,
        status="resolved" if closed else "open",
        page=page,
    )
    return templates.TemplateResponse(
        request=request,
        name="moderation.html",
        context={
            "page_title": "Модерация",
            "active_page": "moderation",
            "query": "",
            "reports": reports.model_dump(mode="json"),
            "closed": closed,
            **viewer_context(state),
        },
    )


@router.get("/admin", response_class=HTMLResponse, response_model=None)
def admin(
    request: Request, page: Annotated[int, Query(ge=1, le=10000)] = 1
) -> HTMLResponse | RedirectResponse:
    state = optional_session(request)
    if state is None or state.user is None:
        return RedirectResponse("/login", status_code=303)
    audit = list_audit(request.app.state.engine, state.user.id, page=page)
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "page_title": "Управление платформой",
            "active_page": "admin",
            "query": "",
            "audit": audit.model_dump(mode="json"),
            **viewer_context(state),
        },
    )
