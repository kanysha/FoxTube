import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Connection, Engine, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.auth.models import AuthRateLimit, AuthSession, Channel, User
from app.auth.passwords import hash_password, verify_password
from app.auth.rate_limits import check_limits
from app.auth.schemas import RegisterInput, SessionView, UserView

COOKIE_NAME = "foxtube_session"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
USER_COLUMNS = (User.id, User.email, User.display_name, User.role, Channel.handle)


@dataclass(frozen=True, repr=False)
class SessionContext:
    session_id: UUID
    token: str
    expires_at: datetime
    user: UserView | None
    issued: bool = False

    @property
    def csrf_token(self) -> str:
        return hmac.new(
            self.token.encode(), b"foxtube-csrf-v1", hashlib.sha256
        ).hexdigest()

    @property
    def token_hash(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()

    def view(self) -> SessionView:
        return SessionView(
            user=self.user, csrf_token=self.csrf_token, expires_at=self.expires_at
        )


def new_session(user: UserView | None = None) -> SessionContext:
    return SessionContext(
        session_id=uuid4(),
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(UTC)
        + (timedelta(days=7) if user else timedelta(minutes=30)),
        user=user,
        issued=True,
    )


def store_session(connection: Connection, state: SessionContext) -> None:
    connection.execute(
        insert(AuthSession).values(
            id=state.session_id,
            token_hash=state.token_hash,
            user_id=state.user.id if state.user else None,
            expires_at=state.expires_at,
        )
    )


def load_session(engine: Engine, token: str | None) -> SessionContext | None:
    if token is None or not TOKEN_PATTERN.fullmatch(token):
        return None
    with engine.connect() as connection:
        row = connection.execute(
            select(AuthSession.id, AuthSession.expires_at, *USER_COLUMNS)
            .select_from(AuthSession)
            .outerjoin(User, User.id == AuthSession.user_id)
            .outerjoin(Channel, Channel.user_id == User.id)
            .where(
                AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > func.now(),
            )
        ).first()
    if row is None:
        return None
    session_id, expiry, user_id, email, name, role, handle = row
    user = (
        UserView(
            id=user_id, email=email, display_name=name, role=role, channel_handle=handle
        )
        if user_id is not None
        else None
    )
    return SessionContext(session_id, token, expiry, user)


def anonymous_session(engine: Engine, client_ip: str) -> SessionContext:
    check_limits(engine, [("session-ip", client_ip, 120)])
    state = new_session()
    with engine.begin() as connection:
        store_session(connection, state)
    return state


def consume_session(connection: Connection, state: SessionContext) -> None:
    # Lock and recheck within the mutation transaction: exactly one rotation wins.
    current = connection.scalar(
        select(AuthSession.id)
        .where(
            AuthSession.id == state.session_id,
            AuthSession.token_hash == state.token_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
        .with_for_update()
    )
    if current is None:
        raise HTTPException(403, "Сессия истекла. Обновите страницу.")
    connection.execute(
        update(AuthSession)
        .where(AuthSession.id == current)
        .values(revoked_at=func.now())
    )


def register(
    engine: Engine, old: SessionContext, data: RegisterInput, client_ip: str
) -> SessionContext:
    email = str(data.email)
    check_limits(engine, [("auth-ip", client_ip, 100), ("register-email", email, 5)])
    encoded = hash_password(data.password.get_secret_value())
    user = UserView(
        id=uuid4(),
        email=email,
        display_name=data.display_name,
        role="user",
        channel_handle=f"fox-{uuid4().hex[:12]}",
    )
    state = new_session(user)
    try:
        with engine.begin() as connection:
            consume_session(connection, old)
            connection.execute(
                insert(User).values(
                    id=user.id,
                    email=email,
                    password_hash=encoded,
                    display_name=data.display_name,
                )
            )
            connection.execute(
                insert(Channel).values(
                    id=uuid4(),
                    user_id=user.id,
                    handle=user.channel_handle,
                    title=user.display_name,
                )
            )
            store_session(connection, state)
    except IntegrityError as exc:
        diagnostic = getattr(exc.orig, "diag", None)
        if getattr(diagnostic, "constraint_name", None) == "users_email_key":
            raise HTTPException(409, "Этот email уже зарегистрирован.") from None
        raise
    return state


def login(
    engine: Engine, old: SessionContext, email: str, password: str, client_ip: str
) -> SessionContext:
    check_limits(engine, [("auth-ip", client_ip, 100), ("login-email", email, 10)])
    with engine.connect() as connection:
        row = connection.execute(
            select(*USER_COLUMNS, User.password_hash)
            .select_from(User)
            .join(Channel, Channel.user_id == User.id)
            .where(User.email == email)
        ).first()
    # No connection is held while Argon2 runs; unknown emails do the same work.
    encoded = row[-1] if row else None
    valid, replacement = verify_password(password, encoded)
    if not valid or row is None:
        raise HTTPException(401, "Неверный email или пароль.")
    user = UserView(
        id=row[0], email=row[1], display_name=row[2], role=row[3], channel_handle=row[4]
    )
    state = new_session(user)
    with engine.begin() as connection:
        consume_session(connection, old)
        if replacement:
            connection.execute(
                update(User)
                .where(User.id == user.id, User.password_hash == encoded)
                .values(password_hash=replacement)
            )
        store_session(connection, state)
    return state


def logout(engine: Engine, old: SessionContext) -> SessionContext:
    # Anonymous logout is idempotent and cannot manufacture session rows.
    # A signed-in user can always revoke a session, even after a shared IP limit.
    if old.user is None:
        return old
    state = new_session()
    with engine.begin() as connection:
        consume_session(connection, old)
        store_session(connection, state)
    return state


def cleanup_auth(connection: Connection) -> None:
    # Small batches leave the worker free for heartbeat and future video jobs.
    cutoff = func.now() - timedelta(days=1)
    session_ids = (
        select(AuthSession.id)
        .where(or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff))
        .limit(500)
    )
    connection.execute(delete(AuthSession).where(AuthSession.id.in_(session_ids)))
    limit_keys = (
        select(AuthRateLimit.key)
        .where(AuthRateLimit.expires_at < func.now())
        .limit(500)
    )
    connection.execute(delete(AuthRateLimit).where(AuthRateLimit.key.in_(limit_keys)))
