import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException

# OWASP minimum Argon2id profile; at most two 19 MiB operations per API process.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_slots = BoundedSemaphore(2)
_dummy_hash = _hasher.hash(secrets.token_urlsafe(32))


@contextmanager
def password_slot() -> Iterator[None]:
    if not _slots.acquire(blocking=False):
        raise HTTPException(
            503, "Сервис занят. Повторите попытку.", headers={"Retry-After": "1"}
        )
    try:
        yield
    finally:
        _slots.release()


def hash_password(password: str) -> str:
    with password_slot():
        return _hasher.hash(password)


def verify_password(password: str, encoded: str | None) -> tuple[bool, str | None]:
    with password_slot():
        try:
            _hasher.verify(encoded or _dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, None
        if encoded is None:
            return False, None
        replacement = (
            _hasher.hash(password) if _hasher.check_needs_rehash(encoded) else None
        )
        return True, replacement
