"""Shared error pages; service endpoints retain their machine-readable contracts."""

from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.auth.dependencies import optional_session
from app.core.security_headers import set_security_headers
from app.web.routes import templates, viewer_context


def is_page(request: Request) -> bool:
    path = request.url.path
    return path != "/openapi.json" and not any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ("/api", "/media", "/health", "/static", "/internal-media")
    )


def error_page(
    request: Request, status: int, headers: Mapping[str, str] | None = None
) -> HTMLResponse:
    title, message = {
        401: ("Войдите в аккаунт", "Для этой страницы нужен вход в аккаунт."),
        403: ("Доступ ограничен", "У вашего аккаунта нет доступа к этой странице."),
        404: (
            "Страница недоступна",
            "Возможно, адрес изменился или у вас нет доступа. "
            "Выберите другое видео на главной или воспользуйтесь поиском.",
        ),
        405: ("Не удалось открыть страницу", "Попробуйте перейти на неё по ссылке."),
        422: (
            "Проверьте адрес",
            "В ссылке или параметрах поиска есть ошибка. "
            "Попробуйте найти видео заново.",
        ),
        429: (
            "Слишком много запросов",
            "Подождите немного и попробуйте открыть страницу снова.",
        ),
        503: (
            "Сервис временно недоступен",
            "Не удалось загрузить страницу. Попробуйте немного позже.",
        ),
    }.get(
        status,
        ("Не удалось загрузить страницу", "Попробуйте ещё раз немного позже."),
    )
    # Error handling must not create sessions or retry a failing database.
    state = None
    if status < 500:
        try:
            state = optional_session(request)
        except SQLAlchemyError:
            pass
    response = templates.TemplateResponse(
        request=request,
        name="error.html",
        status_code=status,
        headers=headers,
        context={
            "page_title": title,
            "error_status": status,
            "error_title": title,
            "error_message": message,
            "active_page": "",
            "query": "",
            **viewer_context(state),
        },
    )
    # The generic 500 handler runs outside the regular middleware stack.
    set_security_headers(
        response.headers,
        request.url.path,
        public_https=request.app.state.settings.public_origin.scheme == "https",
    )
    return response


def http_error(request: Request, error: HTTPException) -> Response:
    if is_page(request):
        return error_page(request, error.status_code, error.headers)
    if error.status_code in (204, 304):
        return Response(status_code=error.status_code, headers=error.headers)
    return JSONResponse(
        {"detail": error.detail},
        status_code=error.status_code,
        headers=error.headers,
    )


def validation_error(request: Request, error: RequestValidationError) -> Response:
    if is_page(request):
        return error_page(request, 422)
    # Pydantic's input/context can contain the submitted password.
    details = [
        {key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()
    ]
    return JSONResponse({"detail": details}, status_code=422)


def database_error(request: Request, error: SQLAlchemyError) -> Response:
    if is_page(request):
        return error_page(request, 503)
    return JSONResponse(
        {"detail": "Сервис временно недоступен. Повторите позже."},
        status_code=503,
    )


def unexpected_error(request: Request, error: Exception) -> Response:
    if is_page(request):
        return error_page(request, 500)
    response = JSONResponse(
        {"detail": "Не удалось выполнить запрос. Повторите позже."}, status_code=500
    )
    set_security_headers(
        response.headers,
        request.url.path,
        public_https=request.app.state.settings.public_origin.scheme == "https",
    )
    return response
