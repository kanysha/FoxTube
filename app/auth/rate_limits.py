import hashlib
import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import Engine, case, func
from sqlalchemy.dialects.postgresql import insert

from app.auth.models import AuthRateLimit

WINDOW = timedelta(minutes=15)


def rate_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def check_limits(engine: Engine, limits: list[tuple[str, str, int]]) -> None:
    retry_after = 0
    # Commit attempts independently of authentication, including failed passwords.
    with engine.begin() as connection:
        for kind, value, maximum in sorted(limits):
            statement = insert(AuthRateLimit).values(
                key=rate_key(kind, value), attempts=1, expires_at=func.now() + WINDOW
            )
            expired = AuthRateLimit.expires_at <= func.now()
            attempts, expiry = connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[AuthRateLimit.key],
                    set_={
                        "attempts": case(
                            (expired, 1), else_=AuthRateLimit.attempts + 1
                        ),
                        "expires_at": case(
                            (expired, func.now() + WINDOW),
                            else_=AuthRateLimit.expires_at,
                        ),
                    },
                ).returning(AuthRateLimit.attempts, AuthRateLimit.expires_at)
            ).one()
            if attempts > maximum:
                retry_after = max(
                    retry_after,
                    math.ceil((expiry - datetime.now(UTC)).total_seconds()),
                    1,
                )
    if retry_after:
        raise HTTPException(
            429,
            "Слишком много попыток. Повторите позже.",
            headers={"Retry-After": str(retry_after)},
        )
