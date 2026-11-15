import hashlib
import os
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from test_auth_integration import (
    TEST_PASSWORD,
    AuthBrowser,
    expect_status,
    new_email,
    require,
    runtime_engine,
)
from test_auth_integration import auth_database as auth_database
from test_auth_integration import auth_settings as auth_settings
from test_auth_integration import make_browser as make_browser

from app.auth import account, recovery, service
from app.auth.models import PasswordResetToken
from app.auth.rate_limits import rate_key
from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration
NEW_PASSWORD = "New-disposable-passphrase-43!"


def login(browser: AuthBrowser, email: str, password: str = TEST_PASSWORD) -> None:
    response = browser.post(
        "/api/v1/auth/login", {"email": email, "password": password}
    )
    expect_status(response, 200)
    browser.read_session(response)


def session_id(browser: AuthBrowser) -> UUID:
    response = browser.client.get("/api/v1/auth/sessions")
    expect_status(response, 200)
    current = [item for item in response.json()["items"] if item["current"]]
    require(len(current) == 1, "Session list must identify the current session")
    return UUID(current[0]["id"])


def seed_token(engine: Engine, user_id: str, *, expired: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            PasswordResetToken.__table__.insert().values(
                id=uuid4(),
                user_id=UUID(user_id),
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                created_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1)
                if expired
                else now + timedelta(minutes=30),
            )
        )
    return token


def test_password_change_rotates_and_revokes(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    owner, other = make_browser(), make_browser()
    user = owner.register()
    login(other, user["email"])
    old_cookie, old_csrf = owner.cookie(), owner.csrf_token
    outstanding = seed_token(auth_database, user["id"])
    wrong = owner.post(
        "/api/v1/auth/password/change",
        {"current_password": NEW_PASSWORD, "new_password": NEW_PASSWORD},
    )
    expect_status(wrong, 403)
    expect_status(other.client.get("/api/v1/auth/me"), 200)
    same = owner.post(
        "/api/v1/auth/password/change",
        {"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD},
    )
    expect_status(same, 422)
    changed = owner.post(
        "/api/v1/auth/password/change",
        {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )
    expect_status(changed, 200)
    owner.read_session(changed)
    require(
        owner.cookie() != old_cookie and owner.csrf_token != old_csrf,
        "Password change did not rotate session and CSRF",
    )
    expect_status(owner.client.get("/api/v1/auth/me"), 200)
    expect_status(other.client.get("/api/v1/auth/me"), 401)
    probe = make_browser()
    expect_status(
        probe.post(
            "/api/v1/auth/password/reset",
            {"token": outstanding, "new_password": TEST_PASSWORD},
        ),
        400,
    )
    expect_status(
        probe.post(
            "/api/v1/auth/login", {"email": user["email"], "password": TEST_PASSWORD}
        ),
        401,
    )
    login(probe, user["email"], NEW_PASSWORD)


def test_session_ownership_revocation_and_csrf(
    make_browser: Callable[[], AuthBrowser],
) -> None:
    owner, one, two, stranger = [make_browser() for _ in range(4)]
    user, foreign = owner.register(), stranger.register()
    login(one, user["email"])
    login(two, user["email"])
    own_id, one_id, foreign_id = (
        session_id(owner),
        session_id(one),
        session_id(stranger),
    )
    listed = owner.client.get("/api/v1/auth/sessions").json()
    require(
        len(listed["items"]) == 3, "Session list leaked or missed an account session"
    )
    require(
        all(
            set(item) == {"id", "created_at", "expires_at", "current"}
            for item in listed["items"]
        ),
        "Session list exposes credential fields",
    )
    require(foreign["id"] != user["id"], "Fixture must use separate accounts")
    headers = {"Origin": owner.origin, "X-CSRF-Token": owner.csrf_token}
    expect_status(owner.client.delete(f"/api/v1/auth/sessions/{one_id}"), 403)
    expect_status(
        owner.client.delete(f"/api/v1/auth/sessions/{foreign_id}", headers=headers), 404
    )
    expect_status(
        owner.client.delete(f"/api/v1/auth/sessions/{own_id}", headers=headers), 409
    )
    expect_status(
        owner.client.delete(f"/api/v1/auth/sessions/{one_id}", headers=headers), 204
    )
    expect_status(one.client.get("/api/v1/auth/me"), 401)
    expect_status(two.client.get("/api/v1/auth/me"), 200)
    response = owner.post("/api/v1/auth/sessions/revoke-others", {})
    expect_status(response, 200)
    require(response.json()["revoked"] == 1, "Revoke others count is incorrect")
    expect_status(two.client.get("/api/v1/auth/me"), 401)
    expect_status(owner.client.get("/api/v1/auth/me"), 200)
    expect_status(stranger.client.get("/api/v1/auth/me"), 200)
    anon = make_browser()
    expect_status(anon.client.get("/api/v1/auth/sessions"), 401)
    expect_status(anon.post("/api/v1/auth/sessions/revoke-others", {}), 401)


def test_recovery_without_smtp_is_uniformly_unavailable(
    make_browser: Callable[[], AuthBrowser],
) -> None:
    owner = make_browser()
    user = owner.register()
    probe = make_browser()
    responses = [
        probe.post("/api/v1/auth/password/forgot", {"email": email})
        for email in [user["email"], new_email()]
    ]
    for response in responses:
        expect_status(response, 503)
    require(
        responses[0].json() == responses[1].json(), "Forgot reveals account existence"
    )


def test_reset_is_single_use_revokes_all_without_login(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    owner, other, anon = make_browser(), make_browser(), make_browser()
    user = owner.register()
    login(other, user["email"])
    token = seed_token(auth_database, user["id"])
    second = seed_token(auth_database, user["id"])
    expired = seed_token(auth_database, user["id"], expired=True)
    for invalid in [expired, secrets.token_urlsafe(32)]:
        expect_status(
            anon.post(
                "/api/v1/auth/password/reset",
                {"token": invalid, "new_password": NEW_PASSWORD},
            ),
            400,
        )
    expect_status(
        anon.post(
            "/api/v1/auth/password/reset",
            {"token": token, "new_password": NEW_PASSWORD},
        ),
        200,
    )
    for used in [token, second]:
        expect_status(
            anon.post(
                "/api/v1/auth/password/reset",
                {"token": used, "new_password": TEST_PASSWORD},
            ),
            400,
        )
    for browser in [owner, other, anon]:
        expect_status(browser.client.get("/api/v1/auth/me"), 401)
    login(anon, user["email"], NEW_PASSWORD)


def test_concurrent_reset_has_one_winner(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    owner, first, second = make_browser(), make_browser(), make_browser()
    user = owner.register()
    token = seed_token(auth_database, user["id"])
    ready = Barrier(2)

    def reset(browser: AuthBrowser) -> int:
        ready.wait(timeout=5)
        return browser.post(
            "/api/v1/auth/password/reset",
            {"token": token, "new_password": NEW_PASSWORD},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(reset, [first, second]))
    require(statuses == [200, 400], "Concurrent reset consumed the token twice")


def test_delivery_hashes_token_and_failure_invalidates(
    make_browser: Callable[[], AuthBrowser],
    auth_settings: Settings,
    auth_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_browser().register()
    api = runtime_engine(auth_settings, "api")
    settings = auth_settings.model_copy(
        update={"smtp_host": "smtp.invalid", "smtp_from": "noreply@example.com"}
    )
    delivered: list[str] = []

    def capture(settings: Settings, email: str, token: str) -> None:
        require(api.pool.checkedout() == 0, "SMTP retained a database connection")
        delivered.append(token)

    monkeypatch.setattr(recovery.mail, "send_recovery", capture)
    try:
        recovery.deliver_recovery(api, settings, new_email())
        require(not delivered, "Recovery sent mail for an unknown account")
        recovery.deliver_recovery(api, settings, user["email"])
        require(len(delivered) == 1, "Recovery did not deliver one credential")
        with auth_database.connect() as connection:
            digest = connection.scalar(
                text(
                    "SELECT token_hash FROM password_reset_tokens "
                    "WHERE user_id=:id AND used_at IS NULL"
                ),
                {"id": user["id"]},
            )
        require(
            digest == hashlib.sha256(delivered[0].encode()).hexdigest(),
            "Recovery persisted an incorrect credential digest",
        )

        def failure(settings: Settings, email: str, token: str) -> None:
            raise OSError("not included in logs")

        monkeypatch.setattr(recovery.mail, "send_recovery", failure)
        recovery.deliver_recovery(api, settings, user["email"])
        with auth_database.connect() as connection:
            active = connection.scalar(
                text(
                    "SELECT count(*) FROM password_reset_tokens "
                    "WHERE user_id=:id AND used_at IS NULL"
                ),
                {"id": user["id"]},
            )
        require(active == 0, "Failed delivery left a usable reset credential")
    finally:
        api.dispose()


def test_login_rechecks_hash_after_concurrent_password_change(
    make_browser: Callable[[], AuthBrowser],
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, anon = make_browser(), make_browser()
    user = owner.register()
    api = runtime_engine(auth_settings, "api")
    old = service.load_session(api, owner.cookie())
    anonymous = service.load_session(api, anon.cookie())
    require(old is not None and anonymous is not None, "Fixture sessions missing")
    verified, resume = Event(), Event()
    original_verify = service.verify_password

    def pause_verify(password: str, encoded: str | None) -> tuple[bool, str | None]:
        result = original_verify(password, encoded)
        verified.set()
        require(resume.wait(timeout=15), "Concurrent login did not resume")
        return result

    monkeypatch.setattr(service, "verify_password", pause_verify)

    def login_paused() -> int:
        try:
            service.login(api, anonymous, user["email"], TEST_PASSWORD, "login-race")
        except HTTPException as error:
            return error.status_code
        return 200

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(login_paused)
            try:
                require(verified.wait(timeout=10), "Login did not verify password")
                account.change_password(
                    api, old, TEST_PASSWORD, NEW_PASSWORD, "change-race"
                )
            finally:
                resume.set()
            require(
                pending.result(timeout=10) == 401, "Old password created a late session"
            )
    finally:
        api.dispose()


def test_stale_session_cannot_revoke_after_revocation(
    make_browser: Callable[[], AuthBrowser], auth_settings: Settings
) -> None:
    owner, other = make_browser(), make_browser()
    user = owner.register()
    login(other, user["email"])
    api = runtime_engine(auth_settings, "api")
    try:
        stale = service.load_session(api, other.cookie())
        require(stale is not None, "Fixture session missing")
        expect_status(owner.post("/api/v1/auth/sessions/revoke-others", {}), 200)
        with pytest.raises(HTTPException) as failure:
            account.revoke_others(api, stale)
        require(
            failure.value.status_code == 401,
            "Stale session authorized account mutation",
        )
    finally:
        api.dispose()


def test_worker_cannot_read_recovery_credentials_and_can_cleanup(
    auth_settings: Settings,
) -> None:
    worker = runtime_engine(auth_settings, "worker")
    try:
        with worker.connect() as connection:
            with pytest.raises(DBAPIError) as failure:
                connection.execute(
                    text("SELECT token_hash FROM password_reset_tokens LIMIT 1")
                )
            require(
                failure.value.orig.sqlstate == "42501",
                "Worker can read recovery digests",
            )
            connection.rollback()
        with worker.begin() as connection:
            service.cleanup_auth(connection)
    finally:
        worker.dispose()


def test_session_pagination_filters_expired_and_foreign_rows(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    owner, stranger = make_browser(), make_browser()
    user = owner.register()
    stranger.register()
    current = session_id(owner)
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid4(),
            "hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            "user": user["id"],
            "created": now - timedelta(hours=2, seconds=index),
            "expiry": now + timedelta(days=1),
            "revoked": None,
        }
        for index in range(20)
    ]
    for state in ["expired", "revoked"]:
        rows.append(
            {
                "id": uuid4(),
                "hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                "user": user["id"],
                "created": now - timedelta(minutes=5),
                "expiry": now - timedelta(minutes=1)
                if state == "expired"
                else now + timedelta(days=1),
                "revoked": now if state == "revoked" else None,
            }
        )
    try:
        with auth_database.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, token_hash, user_id, created_at, expires_at, revoked_at) "
                    "VALUES (:id, :hash, :user, :created, :expiry, :revoked)"
                ),
                rows,
            )
        pages = [
            owner.client.get("/api/v1/auth/sessions", params={"page": number}).json()
            for number in [1, 2]
        ]
        require(
            len(pages[0]["items"]) == 20
            and pages[0]["has_more"]
            and len(pages[1]["items"]) == 1
            and not pages[1]["has_more"],
            "Session filtering must precede pagination",
        )
        ids = {UUID(item["id"]) for page in pages for item in page["items"]}
        require(
            ids == {current, *(row["id"] for row in rows[:20])},
            "Session pagination leaked or omitted records",
        )
    finally:
        with auth_database.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE id = ANY(:ids)"),
                {"ids": [row["id"] for row in rows]},
            )


def test_recovery_rate_limit_and_reset_csrf(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    email = new_email()
    key = rate_key("recovery-email", email)
    with auth_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO auth_rate_limits (key,attempts,expires_at) "
                "VALUES (:key,3,now()+interval '15 minutes')"
            ),
            {"key": key},
        )
    try:
        limited = browser.post("/api/v1/auth/password/forgot", {"email": email})
        expect_status(limited, 429)
        retry = limited.headers.get("retry-after", "")
        require(
            retry.isdecimal() and 0 < int(retry) <= 900, "Missing recovery retry limit"
        )
        forbidden = browser.client.post(
            "/api/v1/auth/password/reset",
            json={"token": secrets.token_urlsafe(32), "new_password": NEW_PASSWORD},
            headers={"Origin": browser.origin},
        )
        expect_status(forbidden, 403)
        require(NEW_PASSWORD not in forbidden.text, "Reset error disclosed password")
    finally:
        with auth_database.begin() as connection:
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE key=:key"), {"key": key}
            )


def test_forgot_background_delivery_to_successful_reset(
    make_browser: Callable[[], AuthBrowser],
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_browser()
    user = owner.register()
    delivered: list[str] = []

    def capture(settings: Settings, email: str, token: str) -> None:
        require(email == user["email"], "Recovery targeted a different account")
        delivered.append(token)

    monkeypatch.setattr(recovery.mail, "send_recovery", capture)
    settings = auth_settings.model_copy(
        update={
            "db_user": "foxtube_api",
            "db_password": SecretStr(os.environ["FOXTUBE_TEST_API_DB_PASSWORD"]),
            "smtp_host": "smtp.invalid",
            "smtp_from": "noreply@example.com",
        }
    )
    origin = str(settings.public_origin).rstrip("/")
    with TestClient(create_app(settings), base_url=origin) as browser:
        session = browser.get("/api/v1/auth/session")
        require(session.status_code == 200, "In-process recovery session failed")
        headers = {"Origin": origin, "X-CSRF-Token": session.json()["csrf_token"]}
        response = browser.post(
            "/api/v1/auth/password/forgot",
            json={"email": user["email"]},
            headers=headers,
        )
        require(response.status_code == 202, "Configured recovery request failed")
        require(len(delivered) == 1, "Background delivery did not issue a link")
        require(
            delivered[0] not in response.text, "Forgot response exposed a credential"
        )
        unknown = browser.post(
            "/api/v1/auth/password/forgot",
            json={"email": new_email()},
            headers=headers,
        )
        require(
            unknown.status_code == 202 and unknown.json() == response.json(),
            "Configured recovery reveals account existence",
        )
        changed = browser.post(
            "/api/v1/auth/password/reset",
            json={"token": delivered[0], "new_password": NEW_PASSWORD},
            headers=headers,
        )
        require(changed.status_code == 200, "Delivered link could not reset password")
        require(
            "set-cookie" not in changed.headers, "Reset must not authenticate the user"
        )
        require(
            browser.get("/api/v1/auth/me").status_code == 401, "Reset logged user in"
        )
    expect_status(owner.client.get("/api/v1/auth/me"), 401)
