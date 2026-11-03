from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.auth import service
from app.auth.dependencies import (
    client_ip,
    ensure_session,
    mutation_session,
    required_user,
    set_session_cookie,
)
from app.auth.schemas import (
    LoginInput,
    LogoutInput,
    RegisterInput,
    SessionView,
    UserView,
)
from app.auth.service import SessionContext

router = APIRouter(prefix="/api/v1/auth", tags=["Учётная запись"])
MutationSession = Annotated[SessionContext, Depends(mutation_session)]


def session_response(
    request: Request, response: Response, state: SessionContext
) -> SessionView:
    if state.issued:
        set_session_cookie(response, state, request.app.state.settings)
    return state.view()


@router.get("/session", response_model=SessionView)
def session(request: Request, response: Response) -> SessionView:
    """Получить CSRF-токен; анонимная сессия создаётся при первом обращении."""
    return session_response(request, response, ensure_session(request))


@router.post("/register", response_model=SessionView, status_code=201)
def register(
    request: Request, response: Response, data: RegisterInput, state: MutationSession
) -> SessionView:
    """Регистрация и вход. Требуются cookie, Origin и X-CSRF-Token из /session."""
    current = service.register(
        request.app.state.engine, state, data, client_ip(request)
    )
    return session_response(request, response, current)


@router.post("/login", response_model=SessionView)
def login(
    request: Request, response: Response, data: LoginInput, state: MutationSession
) -> SessionView:
    """Вход с заменой сессии. Требуются Origin и X-CSRF-Token из /session."""
    current = service.login(
        request.app.state.engine,
        state,
        str(data.email),
        data.password.get_secret_value(),
        client_ip(request),
    )
    return session_response(request, response, current)


@router.post("/logout", response_model=SessionView)
def logout(
    request: Request, response: Response, data: LogoutInput, state: MutationSession
) -> SessionView:
    """Отозвать текущую сессию. Требуются Origin и X-CSRF-Token из /session."""
    return session_response(
        request, response, service.logout(request.app.state.engine, state)
    )


@router.get("/me", response_model=UserView)
def me(user: Annotated[UserView, Depends(required_user)]) -> UserView:
    return user
