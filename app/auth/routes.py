from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from app.auth import account, recovery, service
from app.auth.dependencies import (
    client_ip,
    ensure_session,
    mutation_session,
    required_session,
    required_user,
    set_session_cookie,
)
from app.auth.schemas import (
    AuthMessageView,
    LoginInput,
    LogoutInput,
    PasswordChangeInput,
    PasswordForgotInput,
    PasswordResetInput,
    RegisterInput,
    RevokedSessionsView,
    SessionListView,
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


@router.post("/password/change", response_model=SessionView)
def change_password(
    request: Request,
    response: Response,
    data: PasswordChangeInput,
    state: MutationSession,
) -> SessionView:
    """Сменить пароль, заменить текущую сессию и завершить остальные."""
    current = account.change_password(
        request.app.state.engine,
        state,
        data.current_password.get_secret_value(),
        data.new_password.get_secret_value(),
        client_ip(request),
    )
    return session_response(request, response, current)


@router.get("/sessions", response_model=SessionListView)
def sessions(
    request: Request,
    state: Annotated[SessionContext, Depends(required_session)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> SessionListView:
    """Активные сеансы аккаунта; секретные токены не возвращаются."""
    return account.list_sessions(request.app.state.engine, state, page)


@router.post("/sessions/revoke-others", response_model=RevokedSessionsView)
def revoke_others(
    request: Request, data: LogoutInput, state: MutationSession
) -> RevokedSessionsView:
    return RevokedSessionsView(
        revoked=account.revoke_others(request.app.state.engine, state)
    )


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(
    request: Request, session_id: UUID, state: MutationSession
) -> Response:
    account.revoke_session(request.app.state.engine, state, session_id)
    return Response(status_code=204)


@router.post("/password/forgot", response_model=AuthMessageView, status_code=202)
def forgot_password(
    request: Request,
    data: PasswordForgotInput,
    state: MutationSession,
    tasks: BackgroundTasks,
) -> AuthMessageView:
    """Запросить одноразовую ссылку без раскрытия существования аккаунта."""
    engine, settings = request.app.state.engine, request.app.state.settings
    email = str(data.email)
    recovery.prepare_recovery(engine, settings, email, client_ip(request))
    tasks.add_task(recovery.deliver_recovery, engine, settings, email)
    return AuthMessageView(message=recovery.GENERIC_MESSAGE)


@router.post("/password/reset", response_model=AuthMessageView)
def reset_password(
    request: Request, data: PasswordResetInput, state: MutationSession
) -> AuthMessageView:
    """Заменить пароль одноразовой ссылкой и завершить все сеансы аккаунта."""
    recovery.reset_password(
        request.app.state.engine,
        data.token.get_secret_value(),
        data.new_password.get_secret_value(),
        client_ip(request),
    )
    return AuthMessageView(message="Пароль изменён. Войдите с новым паролем.")
