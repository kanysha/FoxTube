import hashlib
import json
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from threading import Barrier
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.db import make_engine

pytestmark = pytest.mark.integration

COOKIE_NAME = "foxtube_session"
TEST_PASSWORD = "Disposable-test-passphrase-42!"
USER_FIELDS = {"id", "email", "display_name", "role", "channel_handle"}


def require(condition: bool, message: str) -> None:
    # A failed assertion must not include response bodies, passwords or cookies.
    if not condition:
        pytest.fail(message, pytrace=False)


def expect_status(response: httpx.Response, expected: int) -> None:
    require(
        response.status_code == expected,
        f"{response.request.method} {response.request.url.path}: "
        f"expected {expected}, received {response.status_code}",
    )


def new_email() -> str:
    return f"auth-{uuid4().hex}@example.com"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(repr=False)
class AuthBrowser:
    client: httpx.Client = field(repr=False)
    origin: str
    csrf_token: str = field(default="", repr=False)

    def read_session(self, response: httpx.Response) -> dict[str, Any]:
        data = response.json()
        require(
            set(data) == {"user", "csrf_token", "expires_at"},
            "Session response does not match the public schema",
        )
        require(
            isinstance(data["csrf_token"], str) and len(data["csrf_token"]) >= 32,
            "Session did not provide an unpredictable CSRF token",
        )
        self.csrf_token = data["csrf_token"]
        return data

    def cookie(self) -> str:
        token = self.client.cookies.get(COOKIE_NAME)
        require(isinstance(token, str) and len(token) >= 43, "Session cookie missing")
        return str(token)

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return self.client.post(
            path,
            json=payload,
            headers={"Origin": self.origin, "X-CSRF-Token": self.csrf_token},
        )

    def register(
        self, *, email: str | None = None, display_name: str = "Новый автор"
    ) -> dict[str, Any]:
        response = self.post(
            "/api/v1/auth/register",
            {
                "email": email or new_email(),
                "password": TEST_PASSWORD,
                "display_name": display_name,
            },
        )
        expect_status(response, 201)
        user = self.read_session(response)["user"]
        require(isinstance(user, dict), "Registration did not return the new user")
        require(set(user) == USER_FIELDS, "Public user schema exposes extra fields")
        return user


@pytest.fixture(scope="module")
def auth_settings() -> Settings:
    if not os.environ.get("FOXTUBE_TEST_BASE_URL"):
        pytest.fail("Use bash scripts/test.sh for isolated auth integration tests")
    return Settings()


@pytest.fixture(scope="module")
def auth_database(auth_settings: Settings) -> Iterator[Engine]:
    engine = make_engine(auth_settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def make_browser(auth_settings: Settings) -> Iterator[Callable[[], AuthBrowser]]:
    clients: list[httpx.Client] = []

    def create() -> AuthBrowser:
        client = httpx.Client(base_url=os.environ["FOXTUBE_TEST_BASE_URL"], timeout=15)
        clients.append(client)
        browser = AuthBrowser(client, str(auth_settings.public_origin).rstrip("/"))
        response = client.get("/api/v1/auth/session")
        expect_status(response, 200)
        browser.read_session(response)
        return browser

    try:
        yield create
    finally:
        for client in clients:
            client.close()


def assert_copied_cookie_rejected(
    browser: AuthBrowser, old_token: str, old_csrf: str
) -> None:
    with httpx.Client(base_url=browser.client.base_url, timeout=10) as copied:
        response = copied.get(
            "/api/v1/auth/me", headers={"Cookie": f"{COOKIE_NAME}={old_token}"}
        )
        expect_status(response, 401)
        response = copied.post(
            "/api/v1/auth/logout",
            json={},
            headers={
                "Cookie": f"{COOKIE_NAME}={old_token}",
                "Origin": browser.origin,
                "X-CSRF-Token": old_csrf,
            },
        )
        require(
            response.status_code in {401, 403},
            "Rotated or revoked session still authorizes mutations",
        )


def test_anonymous_session_cookie_and_storage(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    response = browser.client.get("/api/v1/auth/session")
    expect_status(response, 200)
    data = browser.read_session(response)
    require(data["user"] is None, "New browser must be anonymous")
    expires_at = datetime.fromisoformat(data["expires_at"])
    remaining = expires_at - datetime.now(UTC)
    require(
        timedelta(minutes=29) < remaining <= timedelta(minutes=31),
        "Anonymous session lifetime must be approximately 30 minutes",
    )
    token = browser.cookie()
    with auth_database.connect() as connection:
        row = connection.execute(
            text("SELECT token_hash, user_id FROM sessions WHERE token_hash = :hash"),
            {"hash": token_hash(token)},
        ).one()
    require(row.user_id is None, "Anonymous session unexpectedly has an owner")
    require(
        row.token_hash != token, "Raw session token must not be stored in PostgreSQL"
    )
    expect_status(browser.client.get("/api/v1/auth/me"), 401)

    # A fresh browser guarantees Set-Cookie even when valid sessions are reused.
    with httpx.Client(base_url=browser.client.base_url, timeout=10) as fresh:
        created = fresh.get("/api/v1/auth/session")
    expect_status(created, 200)
    cookie: SimpleCookie[str] = SimpleCookie()
    cookie.load(created.headers.get("set-cookie", ""))
    require(COOKIE_NAME in cookie, "Session endpoint must set its cookie")
    morsel = cookie[COOKIE_NAME]
    require(bool(morsel["httponly"]), "Session cookie must be HttpOnly")
    require(
        morsel["samesite"].casefold() == "lax", "Session cookie must use SameSite=Lax"
    )
    require(morsel["path"] == "/", "Session cookie must cover all application paths")
    require(not morsel["domain"], "Session cookie must be scoped to the current host")


def test_register_login_logout_rotate_and_revoke_sessions(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    anonymous_token = browser.cookie()
    anonymous_csrf = browser.csrf_token
    email = new_email()
    user = browser.register(email=email)
    require(user["email"] == email and user["role"] == "user", "Incorrect new user")
    require(bool(user["channel_handle"]), "Registration must create a channel")
    require(browser.cookie() != anonymous_token, "Registration must rotate the session")
    assert_copied_cookie_rejected(browser, anonymous_token, anonymous_csrf)
    response = browser.client.get("/api/v1/auth/me")
    expect_status(response, 200)
    require(
        response.json() == user, "Authenticated identity does not match registration"
    )

    with auth_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT u.id, u.password_hash, u.role, count(c.id) AS channels "
                "FROM users u LEFT JOIN channels c ON c.user_id = u.id "
                "WHERE u.email = :email GROUP BY u.id"
            ),
            {"email": email},
        ).one()
    require(row.password_hash.startswith("$argon2id$"), "Passwords must use Argon2id")
    require(TEST_PASSWORD not in row.password_hash, "Password is stored in clear text")
    require(
        row.role == "user" and row.channels == 1, "User and channel were not atomic"
    )

    authenticated_token = browser.cookie()
    authenticated_csrf = browser.csrf_token
    response = browser.post("/api/v1/auth/logout", {})
    expect_status(response, 200)
    require(
        browser.read_session(response)["user"] is None, "Logout must become anonymous"
    )
    require(browser.cookie() != authenticated_token, "Logout must replace its cookie")
    assert_copied_cookie_rejected(browser, authenticated_token, authenticated_csrf)
    expect_status(browser.client.get("/api/v1/auth/me"), 401)

    login_anonymous_token = browser.cookie()
    login_anonymous_csrf = browser.csrf_token
    response = browser.post(
        "/api/v1/auth/login", {"email": email, "password": TEST_PASSWORD}
    )
    expect_status(response, 200)
    data = browser.read_session(response)
    require(data["user"] == user, "Login returned a different identity")
    require(
        browser.cookie() != login_anonymous_token, "Login must prevent session fixation"
    )
    assert_copied_cookie_rejected(browser, login_anonymous_token, login_anonymous_csrf)
    lifetime = datetime.fromisoformat(data["expires_at"]) - datetime.now(UTC)
    require(
        timedelta(days=6, hours=23) < lifetime <= timedelta(days=7, hours=1),
        "Authenticated session lifetime must be approximately seven days",
    )
    expect_status(browser.client.get("/account"), 200)


def test_html_auth_pages_share_csrf_and_account_escapes_display_name(
    make_browser: Callable[[], AuthBrowser],
) -> None:
    browser = make_browser()
    for path in ("/login", "/register"):
        response = browser.client.get(path)
        expect_status(response, 200)
        require(
            browser.csrf_token in response.text
            and '<meta name="csrf-token"' in response.text,
            "Auth form must expose the current session CSRF token in a meta tag",
        )
    response = browser.client.get("/account")
    require(response.status_code in {302, 303, 307}, "Guest can open the account page")
    require(
        response.headers.get("location", "").split("?", maxsplit=1)[0] == "/login",
        "Guest account access must redirect to login",
    )
    display_name = '<img src=x onerror="alert(1)">'
    browser.register(display_name=display_name)
    response = browser.client.get("/account")
    expect_status(response, 200)
    require(
        display_name not in response.text and "&lt;img" in response.text,
        "Account page must HTML-escape the user-controlled display name",
    )


def test_email_and_display_name_normalization_and_duplicate_registration(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    email = new_email()
    user = browser.register(
        email=f"  {email.upper()}  ", display_name="  Автор канала  "
    )
    require(user["email"] == email, "Email must be stripped and casefolded")
    require(user["display_name"] == "Автор канала", "Display name must be stripped")
    duplicate = make_browser()
    response = duplicate.post(
        "/api/v1/auth/register",
        {"email": email, "password": TEST_PASSWORD, "display_name": "Другой автор"},
    )
    expect_status(response, 409)
    expect_status(duplicate.client.get("/api/v1/auth/me"), 401)
    with auth_database.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM channels c JOIN users u ON u.id = c.user_id "
                "WHERE u.email = :email"
            ),
            {"email": email},
        )
    require(count == 1, "Duplicate registration created another channel")

    response = duplicate.post(
        "/api/v1/auth/login", {"email": f" {email.upper()} ", "password": TEST_PASSWORD}
    )
    expect_status(response, 200)
    require(
        duplicate.read_session(response)["user"] == user,
        "Login did not normalize email",
    )


def test_concurrent_duplicate_registration_is_atomic(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browsers = [make_browser(), make_browser()]
    email = new_email()
    ready = Barrier(2)

    def register(browser: AuthBrowser) -> httpx.Response:
        ready.wait(timeout=5)
        return browser.post(
            "/api/v1/auth/register",
            {"email": email, "password": TEST_PASSWORD, "display_name": "Одновременно"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(register, browsers))
    require(
        sorted(response.status_code for response in responses) == [201, 409],
        "Concurrent registration must have one winner and one conflict",
    )
    with auth_database.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT count(DISTINCT u.id) AS users, "
                "count(DISTINCT c.id) AS channels, "
                "count(DISTINCT s.id) FILTER (WHERE s.revoked_at IS NULL) AS sessions "
                "FROM users u LEFT JOIN channels c ON c.user_id = u.id "
                "LEFT JOIN sessions s ON s.user_id = u.id WHERE u.email = :email"
            ),
            {"email": email},
        ).one()
    require(
        counts == (1, 1, 1), "Concurrent registration left duplicate or partial records"
    )
    for browser, response in zip(browsers, responses, strict=True):
        expect_status(
            browser.client.get("/api/v1/auth/me"),
            200 if response.status_code == 201 else 401,
        )


@pytest.mark.parametrize("path", ["register", "login", "logout"])
def test_mutations_require_origin_and_session_bound_csrf(
    make_browser: Callable[[], AuthBrowser], path: str
) -> None:
    browser = make_browser()
    another = make_browser()
    payload = (
        {"email": new_email(), "password": TEST_PASSWORD} if path != "logout" else {}
    )
    if path == "register":
        payload["display_name"] = "Недоступная регистрация"
    endpoint = f"/api/v1/auth/{path}"
    valid_headers = {"Origin": browser.origin, "X-CSRF-Token": browser.csrf_token}
    header_cases = [
        {"X-CSRF-Token": browser.csrf_token},
        dict(valid_headers, Origin="https://attacker.invalid"),
        dict(valid_headers, Origin=f"{browser.origin}/"),
        {"Origin": browser.origin},
        dict(valid_headers, **{"X-CSRF-Token": "invalid-csrf-token"}),
        dict(valid_headers, **{"X-CSRF-Token": another.csrf_token}),
    ]
    for headers in header_cases:
        response = browser.client.post(endpoint, json=payload, headers=headers)
        expect_status(response, 403)
    response = browser.client.post(
        endpoint,
        json=payload,
        headers={
            b"Origin": browser.origin.encode("ascii"),
            b"X-CSRF-Token": b"\xff" * 64,
        },
    )
    expect_status(response, 403)
    browser.client.cookies.clear()
    response = browser.client.post(endpoint, json=payload, headers=valid_headers)
    require(
        response.status_code in {401, 403},
        "A valid CSRF token without its session cookie must not authorize a mutation",
    )


def test_wrong_password_and_unknown_email_have_identical_errors(
    make_browser: Callable[[], AuthBrowser],
) -> None:
    registered = make_browser()
    user = registered.register()
    browser = make_browser()
    wrong_password = "Incorrect-test-passphrase-42!"
    responses = [
        browser.post("/api/v1/auth/login", {"email": email, "password": wrong_password})
        for email in (user["email"], new_email())
    ]
    for response in responses:
        expect_status(response, 401)
    require(
        responses[0].json() == responses[1].json(),
        "Login errors disclose whether the email exists",
    )
    require(
        all(wrong_password not in response.text for response in responses),
        "Login errors must not echo submitted passwords",
    )
    expect_status(browser.client.get("/api/v1/auth/me"), 401)


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_and_revoked_sessions_cannot_authenticate(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine, state: str
) -> None:
    browser = make_browser()
    browser.register()
    statement = (
        "UPDATE sessions SET created_at = now() - interval '8 days', "
        "expires_at = now() - interval '1 second' "
        "WHERE token_hash = :hash"
        if state == "expired"
        else "UPDATE sessions SET revoked_at = now() WHERE token_hash = :hash"
    )
    with auth_database.begin() as connection:
        connection.execute(text(statement), {"hash": token_hash(browser.cookie())})
    expect_status(browser.client.get("/api/v1/auth/me"), 401)
    response = browser.client.get("/account")
    require(
        response.status_code in {302, 303, 307}, "Revoked identity can open /account"
    )
    require(
        response.headers.get("location", "").split("?", maxsplit=1)[0] == "/login",
        "Unauthenticated account access must redirect to login",
    )


def test_concurrent_logout_consumes_session_only_once(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    user = browser.register()
    original_token = browser.cookie()
    csrf = browser.csrf_token
    ready = Barrier(2)

    def logout(_: int) -> int:
        with httpx.Client(base_url=browser.client.base_url, timeout=15) as copied:
            ready.wait(timeout=5)
            response = copied.post(
                "/api/v1/auth/logout",
                json={},
                headers={
                    "Cookie": f"{COOKIE_NAME}={original_token}",
                    "Origin": browser.origin,
                    "X-CSRF-Token": csrf,
                },
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(logout, range(2)))
    require(
        statuses in ([200, 401], [200, 403]),
        "Concurrent logout must have exactly one success; the other must be 401/403",
    )
    assert_copied_cookie_rejected(browser, original_token, csrf)
    with auth_database.connect() as connection:
        active = connection.scalar(
            text(
                "SELECT count(*) FROM sessions WHERE user_id = :id "
                "AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"id": user["id"]},
        )
    require(active == 0, "Concurrent logout left an authenticated session active")


def test_anonymous_logout_is_idempotent_without_new_sessions(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    original_token = browser.cookie()
    original_csrf = browser.csrf_token
    with auth_database.connect() as connection:
        original_count = connection.scalar(text("SELECT count(*) FROM sessions"))
    for _ in range(3):
        response = browser.post("/api/v1/auth/logout", {})
        expect_status(response, 200)
        data = browser.read_session(response)
        require(data["user"] is None, "Anonymous logout must remain anonymous")
        require(
            browser.cookie() == original_token and browser.csrf_token == original_csrf,
            "Anonymous logout must reuse the existing session",
        )
        require(
            "set-cookie" not in response.headers,
            "Idempotent anonymous logout must not issue another cookie",
        )
    with auth_database.connect() as connection:
        final_count = connection.scalar(text("SELECT count(*) FROM sessions"))
    require(
        final_count == original_count, "Anonymous logout created extra session rows"
    )


def test_current_user_role_is_loaded_from_postgresql(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    browser = make_browser()
    user = browser.register()
    original_token = browser.cookie()
    with auth_database.begin() as connection:
        connection.execute(
            text("UPDATE users SET role = 'moderator' WHERE id = :id"),
            {"id": user["id"]},
        )
    try:
        response = browser.client.get("/api/v1/auth/me")
        expect_status(response, 200)
        require(
            response.json()["role"] == "moderator",
            "Current role must be read from PostgreSQL on the next request",
        )
        require(
            browser.cookie() == original_token,
            "A database role change must take effect without a new session",
        )
    finally:
        with auth_database.begin() as connection:
            connection.execute(
                text("UPDATE users SET role = 'user' WHERE id = :id"),
                {"id": user["id"]},
            )
    response = browser.client.get("/api/v1/auth/me")
    expect_status(response, 200)
    require(response.json()["role"] == "user", "Role downgrade remained stale")


@pytest.mark.parametrize("path", ["register", "login", "logout"])
@pytest.mark.parametrize("transfer", ["content-length", "chunked"])
def test_auth_rejects_oversized_bodies_before_validation(
    make_browser: Callable[[], AuthBrowser], path: str, transfer: str
) -> None:
    browser = make_browser()
    encoded = json.dumps(
        {
            "email": new_email(),
            "password": TEST_PASSWORD,
            "display_name": "Большое тело",
            "padding": "x" * (17 * 1024),
        }
    ).encode()
    headers = {
        "Origin": browser.origin,
        "X-CSRF-Token": browser.csrf_token,
        "Content-Type": "application/json",
    }
    content = (
        iter(encoded[start : start + 4096] for start in range(0, len(encoded), 4096))
        if transfer == "chunked"
        else encoded
    )
    response = browser.client.post(
        f"/api/v1/auth/{path}", content=content, headers=headers
    )
    expect_status(response, 413)
    require(TEST_PASSWORD not in response.text, "Oversized errors echoed the password")


@pytest.mark.parametrize("password_length", [11, 129])
def test_invalid_password_is_not_echoed_or_persisted(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine, password_length: int
) -> None:
    browser = make_browser()
    email = new_email()
    password = "p" * password_length
    response = browser.post(
        "/api/v1/auth/register",
        {"email": email, "password": password, "display_name": "Не создан"},
    )
    expect_status(response, 422)
    require(password not in response.text, "Validation errors echoed the password")
    with auth_database.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM users WHERE email = :email"), {"email": email}
        )
    require(count == 0, "Invalid registration persisted a user")


@pytest.mark.parametrize("extra_field", ["role", "user_id"])
def test_registration_cannot_assign_privilege_or_owner_fields(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine, extra_field: str
) -> None:
    browser = make_browser()
    email = new_email()
    response = browser.post(
        "/api/v1/auth/register",
        {
            "email": email,
            "password": TEST_PASSWORD,
            "display_name": "Не администратор",
            extra_field: "admin" if extra_field == "role" else str(uuid4()),
        },
    )
    expect_status(response, 422)
    require(TEST_PASSWORD not in response.text, "Validation errors echoed the password")
    with auth_database.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM users WHERE email = :email"), {"email": email}
        )
    require(count == 0, "Mass assignment attempt persisted an account")


def runtime_engine(settings: Settings, role: str) -> Engine:
    return make_engine(
        settings.model_copy(
            update={
                "db_user": f"foxtube_{role}",
                "db_password": SecretStr(
                    os.environ[f"FOXTUBE_TEST_{role.upper()}_DB_PASSWORD"]
                ),
            }
        )
    )


def test_runtime_database_roles_restrict_authentication_data(
    auth_settings: Settings, make_browser: Callable[[], AuthBrowser]
) -> None:
    user = make_browser().register()
    api = runtime_engine(auth_settings, "api")
    worker = runtime_engine(auth_settings, "worker")
    try:
        for statement, parameters in (
            (
                "UPDATE users SET role = 'admin' WHERE id = :id",
                {"id": user["id"]},
            ),
            (
                "INSERT INTO users (id, email, password_hash, display_name, role) "
                "VALUES (:id, :email, 'not-a-real-password-hash', "
                "'Forbidden', 'admin')",
                {"id": uuid4(), "email": new_email()},
            ),
            (
                "UPDATE users SET email = :email WHERE id = :id",
                {"id": user["id"], "email": new_email()},
            ),
        ):
            with api.connect() as connection:
                with pytest.raises(DBAPIError) as failure:
                    connection.execute(text(statement), parameters)
                require(
                    failure.value.orig.sqlstate == "42501",
                    "API role must be denied sensitive user writes by PostgreSQL",
                )
                connection.rollback()
        with worker.connect() as connection:
            with pytest.raises(DBAPIError) as failure:
                connection.execute(text("SELECT password_hash FROM users LIMIT 1"))
            require(
                failure.value.orig.sqlstate == "42501",
                "Worker must not read password hashes",
            )
            connection.rollback()
            # Exercise cleanup privileges without deleting another test's records.
            connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": uuid4()}
            )
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE key = :key"),
                {"key": uuid4().hex},
            )
            connection.rollback()
    finally:
        api.dispose()
        worker.dispose()


def test_login_rate_limit_is_shared_in_postgresql(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    email = new_email()
    key = hashlib.sha256(f"login-email:{email}".encode()).hexdigest()
    with auth_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO auth_rate_limits (key, attempts, expires_at) "
                "VALUES (:key, 10, now() + interval '15 minutes') "
                "ON CONFLICT (key) DO UPDATE SET attempts = 10, "
                "expires_at = EXCLUDED.expires_at"
            ),
            {"key": key},
        )
    try:
        # Independent clients exercise the same database bucket, not a local counter.
        for browser in (make_browser(), make_browser()):
            response = browser.post(
                "/api/v1/auth/login", {"email": email, "password": TEST_PASSWORD}
            )
            expect_status(response, 429)
            retry_after = response.headers.get("retry-after", "")
            require(
                retry_after.isdecimal() and 0 < int(retry_after) <= 900,
                "Rate limit must return a bounded Retry-After in seconds",
            )
    finally:
        with auth_database.begin() as connection:
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE key = :key"), {"key": key}
            )


def test_failed_password_attempts_remain_committed(
    make_browser: Callable[[], AuthBrowser], auth_database: Engine
) -> None:
    registered = make_browser()
    user = registered.register()
    email = user["email"]
    key = hashlib.sha256(f"login-email:{email}".encode()).hexdigest()
    with auth_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO auth_rate_limits (key, attempts, expires_at) "
                "VALUES (:key, 9, now() + interval '15 minutes')"
            ),
            {"key": key},
        )
    try:
        browser = make_browser()
        response = browser.post(
            "/api/v1/auth/login",
            {"email": email, "password": "Incorrect-test-passphrase-42!"},
        )
        expect_status(response, 401)
        with auth_database.connect() as connection:
            attempts = connection.scalar(
                text("SELECT attempts FROM auth_rate_limits WHERE key = :key"),
                {"key": key},
            )
        require(attempts == 10, "Incorrect password rolled back its rate-limit attempt")
        response = make_browser().post(
            "/api/v1/auth/login", {"email": email, "password": TEST_PASSWORD}
        )
        expect_status(response, 429)
        expect_status(browser.client.get("/api/v1/auth/me"), 401)
    finally:
        with auth_database.begin() as connection:
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE key = :key"), {"key": key}
            )


def test_worker_cleanup_deletes_old_auth_data_and_preserves_recent_data(
    auth_settings: Settings, auth_database: Engine
) -> None:
    from app.auth.service import cleanup_auth

    now = datetime.now(UTC)
    session_ids = {
        name: uuid4()
        for name in (
            "old_expired",
            "old_revoked",
            "active",
            "recent_expired",
            "recent_revoked",
        )
    }
    lifetimes = {
        "old_expired": (
            now - timedelta(days=4),
            now - timedelta(days=2),
            None,
        ),
        "old_revoked": (
            now - timedelta(days=4),
            now + timedelta(days=3),
            now - timedelta(days=2),
        ),
        "active": (
            now - timedelta(minutes=5),
            now + timedelta(minutes=30),
            None,
        ),
        "recent_expired": (
            now - timedelta(hours=2),
            now - timedelta(hours=1),
            None,
        ),
        "recent_revoked": (
            now - timedelta(hours=2),
            now + timedelta(hours=1),
            now - timedelta(hours=1),
        ),
    }
    rate_keys = {name: uuid4().hex + uuid4().hex for name in ("expired", "active")}
    worker = runtime_engine(auth_settings, "worker")
    try:
        with auth_database.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, token_hash, created_at, expires_at, revoked_at) "
                    "VALUES (:id, :hash, :created_at, :expires_at, :revoked_at)"
                ),
                [
                    {
                        "id": session_ids[name],
                        "hash": uuid4().hex + uuid4().hex,
                        "created_at": created_at,
                        "expires_at": expires_at,
                        "revoked_at": revoked_at,
                    }
                    for name, (created_at, expires_at, revoked_at) in lifetimes.items()
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO auth_rate_limits (key, attempts, expires_at) "
                    "VALUES (:key, 1, :expires_at)"
                ),
                [
                    {
                        "key": rate_keys["expired"],
                        "expires_at": now - timedelta(hours=1),
                    },
                    {
                        "key": rate_keys["active"],
                        "expires_at": now + timedelta(minutes=15),
                    },
                ],
            )
        with worker.begin() as connection:
            require(
                connection.scalar(text("SELECT current_user")) == "foxtube_worker",
                "Cleanup must use the actual restricted worker database role",
            )
            cleanup_auth(connection)
        with auth_database.connect() as connection:
            remaining_sessions = set(
                connection.scalars(
                    text("SELECT id FROM sessions WHERE id = ANY(:ids)"),
                    {"ids": list(session_ids.values())},
                )
            )
            remaining_rates = set(
                connection.scalars(
                    text("SELECT key FROM auth_rate_limits WHERE key = ANY(:keys)"),
                    {"keys": list(rate_keys.values())},
                )
            )
        require(
            remaining_sessions
            == {
                session_ids["active"],
                session_ids["recent_expired"],
                session_ids["recent_revoked"],
            },
            "Cleanup must remove old sessions and preserve active or recent sessions",
        )
        require(
            remaining_rates == {rate_keys["active"]},
            "Cleanup must delete expired rate limits and preserve active limits",
        )
    finally:
        worker.dispose()
        with auth_database.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE id = ANY(:ids)"),
                {"ids": list(session_ids.values())},
            )
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE key = ANY(:keys)"),
                {"keys": list(rate_keys.values())},
            )
