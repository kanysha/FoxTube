"""Account mutations serialize against password changes and session revocation."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Connection, Engine, func, select, update

from app.auth.models import AuthSession, PasswordResetToken, User
from app.auth.passwords import hash_password, verify_password
from app.auth.rate_limits import check_limits
from app.auth.schemas import ActiveSessionView, SessionListView
from app.auth.service import (
    SessionContext,
    consume_session,
    new_session,
    store_session,
)


def require_account(state: SessionContext) -> UUID:
    if state.user is None:
        raise HTTPException(401, "Необходим вход.")
    return state.user.id


def lock_account(connection: Connection, user_id: UUID) -> str:
    encoded = connection.scalar(
        select(User.password_hash).where(User.id == user_id).with_for_update()
    )
    if encoded is None:
        raise HTTPException(401, "Необходим вход.")
    return encoded


def fresh_session(connection: Connection, state: SessionContext) -> None:
    current = connection.scalar(
        select(AuthSession.id).where(
            AuthSession.id == state.session_id,
            AuthSession.token_hash == state.token_hash,
            AuthSession.user_id == require_account(state),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
    )
    if current is None:
        raise HTTPException(401, "Сессия истекла. Войдите снова.")


def revoke_password_tokens(connection: Connection, user_id: UUID) -> None:
    connection.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=func.now())
    )


def revoke_user_sessions(
    connection: Connection, user_id: UUID, *, except_id: UUID | None = None
) -> int:
    statement = update(AuthSession).where(
        AuthSession.user_id == user_id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > func.now(),
    )
    if except_id is not None:
        statement = statement.where(AuthSession.id != except_id)
    return connection.execute(statement.values(revoked_at=func.now())).rowcount


def change_password(
    engine: Engine,
    state: SessionContext,
    current_password: str,
    new_password: str,
    client_ip: str,
) -> SessionContext:
    user_id = require_account(state)
    check_limits(
        engine,
        [("password-ip", client_ip, 50), ("password-user", str(user_id), 10)],
    )
    with engine.connect() as connection:
        previous = connection.scalar(
            select(User.password_hash).where(User.id == user_id)
        )
    valid, _ = verify_password(current_password, previous)
    if not valid:
        raise HTTPException(403, "Текущий пароль указан неверно.")
    if current_password == new_password:
        raise HTTPException(422, "Новый пароль должен отличаться от текущего.")
    encoded = hash_password(new_password)
    replacement = new_session(state.user)
    with engine.begin() as connection:
        if lock_account(connection, user_id) != previous:
            raise HTTPException(409, "Пароль уже изменён. Войдите снова.")
        fresh_session(connection, state)
        consume_session(connection, state)
        connection.execute(
            update(User).where(User.id == user_id).values(password_hash=encoded)
        )
        revoke_user_sessions(connection, user_id)
        revoke_password_tokens(connection, user_id)
        store_session(connection, replacement)
    return replacement


def list_sessions(engine: Engine, state: SessionContext, page: int) -> SessionListView:
    user_id = require_account(state)
    with engine.connect() as connection:
        fresh_session(connection, state)
        rows = connection.execute(
            select(AuthSession.id, AuthSession.created_at, AuthSession.expires_at)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > func.now(),
            )
            .order_by(AuthSession.created_at.desc(), AuthSession.id.desc())
            .offset((page - 1) * 20)
            .limit(21)
        ).all()
    return SessionListView(
        items=[
            ActiveSessionView(
                id=row.id,
                created_at=row.created_at,
                expires_at=row.expires_at,
                current=row.id == state.session_id,
            )
            for row in rows[:20]
        ],
        page=page,
        has_more=len(rows) > 20,
    )


def revoke_session(engine: Engine, state: SessionContext, session_id: UUID) -> None:
    user_id = require_account(state)
    with engine.begin() as connection:
        lock_account(connection, user_id)
        fresh_session(connection, state)
        if state.session_id == session_id:
            raise HTTPException(409, "Для текущего сеанса используйте выход.")
        revoked = connection.execute(
            update(AuthSession)
            .where(
                AuthSession.id == session_id,
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
        ).rowcount
        if not revoked:
            raise HTTPException(404, "Сеанс недоступен.")


def revoke_others(engine: Engine, state: SessionContext) -> int:
    user_id = require_account(state)
    with engine.begin() as connection:
        lock_account(connection, user_id)
        fresh_session(connection, state)
        return revoke_user_sessions(connection, user_id, except_id=state.session_id)
