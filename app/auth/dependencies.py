import hmac
import ipaddress
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, Security
from fastapi.security import APIKeyCookie

from app.auth.schemas import UserView
from app.auth.service import (
    COOKIE_NAME,
    SessionContext,
    anonymous_session,
    load_session,
)
from app.core.config import Settings

session_cookie = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


def client_ip(request: Request) -> str:
    # Only Nginx is published; it overwrites this header, clearing spoofed values.
    candidate = request.headers.get("x-foxtube-client-ip", "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.client.host if request.client else "unknown"


def optional_session(request: Request) -> SessionContext | None:
    return load_session(request.app.state.engine, request.cookies.get(COOKIE_NAME))


def ensure_session(request: Request) -> SessionContext:
    return optional_session(request) or anonymous_session(
        request.app.state.engine, client_ip(request)
    )


def required_session(
    request: Request, token: Annotated[str | None, Security(session_cookie)]
) -> SessionContext:
    state = load_session(request.app.state.engine, token)
    if state is None:
        raise HTTPException(401, "Необходим вход.")
    return state


def mutation_session(
    request: Request, state: Annotated[SessionContext, Depends(required_session)]
) -> SessionContext:
    origin = request.headers.get("origin")
    expected = str(request.app.state.settings.public_origin).rstrip("/")
    supplied = request.headers.get("x-csrf-token", "")
    if (
        origin != expected
        or len(supplied) != 64
        or not supplied.isascii()
        or not hmac.compare_digest(supplied, state.csrf_token)
    ):
        raise HTTPException(403, "Защита формы: обновите страницу и повторите.")
    return state


def required_user(
    state: Annotated[SessionContext, Depends(required_session)],
) -> UserView:
    if state.user is None:
        raise HTTPException(401, "Необходим вход.")
    return state.user


def set_session_cookie(
    response: Response, state: SessionContext, settings: Settings
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        state.token,
        max_age=7 * 86400 if state.user else 1800,
        expires=state.expires_at,
        path="/",
        secure=settings.public_origin.scheme == "https",
        httponly=True,
        samesite="lax",
    )
