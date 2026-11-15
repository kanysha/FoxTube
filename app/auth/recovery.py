"""Short-lived single-use password reset, separate from authentication sessions."""

import hashlib
import logging
import secrets
from datetime import timedelta
from threading import BoundedSemaphore
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import Engine, func, insert, select, update

from app.auth import mail
from app.auth.account import (
    lock_account,
    revoke_password_tokens,
    revoke_user_sessions,
)
from app.auth.models import PasswordResetToken, User
from app.auth.passwords import hash_password
from app.auth.rate_limits import check_limits
from app.auth.service import TOKEN_PATTERN
from app.core.config import Settings

_logger = logging.getLogger(__name__)
_delivery_slots = BoundedSemaphore(2)
GENERIC_MESSAGE = (
    "Если адрес зарегистрирован, вы получите письмо с инструкцией. "
    "Если письмо не пришло, попробуйте позже."
)
INVALID_MESSAGE = "Ссылка недействительна или истекла. Запросите новую."


def prepare_recovery(
    engine: Engine, settings: Settings, email: str, client_ip: str
) -> None:
    check_limits(engine, [("recovery-ip", client_ip, 20), ("recovery-email", email, 3)])
    if not mail.configured(settings):
        raise HTTPException(503, "Восстановление пароля временно недоступно.")


def deliver_recovery(engine: Engine, settings: Settings, email: str) -> None:
    # Run only after the generic response, outside the request's DB transaction.
    if not _delivery_slots.acquire(blocking=False):
        _logger.warning("Password recovery delivery capacity reached")
        return
    token_id = uuid4()
    try:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with engine.begin() as connection:
            user_id = connection.scalar(select(User.id).where(User.email == email))
            if user_id is None:
                return
            lock_account(connection, user_id)
            # At most one outstanding link per account, even concurrent requests.
            revoke_password_tokens(connection, user_id)
            connection.execute(
                insert(PasswordResetToken).values(
                    id=token_id,
                    user_id=user_id,
                    token_hash=digest,
                    expires_at=func.now() + timedelta(minutes=30),
                )
            )
        # Do not hold a database connection during TLS/SMTP.
        mail.send_recovery(settings, email, token)
    except Exception:
        # SMTP failures have the same public result as an unknown account.
        # Never include exception strings, recipient, password or link in logs.
        _logger.warning("Password recovery delivery failed")
        try:
            with engine.begin() as connection:
                connection.execute(
                    update(PasswordResetToken)
                    .where(PasswordResetToken.id == token_id)
                    .values(used_at=func.now())
                )
        except Exception:
            _logger.warning("Password recovery credential cleanup failed")
    finally:
        _delivery_slots.release()


def reset_password(
    engine: Engine, token: str, new_password: str, client_ip: str
) -> None:
    check_limits(engine, [("reset-ip", client_ip, 30)])
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(400, INVALID_MESSAGE)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with engine.connect() as connection:
        user_id = connection.scalar(
            select(PasswordResetToken.user_id).where(
                PasswordResetToken.token_hash == digest,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > func.now(),
            )
        )
    if user_id is None:
        raise HTTPException(400, INVALID_MESSAGE)
    encoded = hash_password(new_password)
    with engine.begin() as connection:
        lock_account(connection, user_id)
        current = connection.scalar(
            select(PasswordResetToken.id).where(
                PasswordResetToken.token_hash == digest,
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > func.now(),
            )
        )
        if current is None:
            raise HTTPException(400, INVALID_MESSAGE)
        connection.execute(
            update(User).where(User.id == user_id).values(password_hash=encoded)
        )
        # Keep the global write order: user, sessions, recovery credentials.
        revoke_user_sessions(connection, user_id)
        revoke_password_tokens(connection, user_id)
