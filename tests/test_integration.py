import os
import subprocess
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.db import make_engine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("FOXTUBE_TEST_BASE_URL"):
        pytest.fail(
            "Use bash scripts/test.sh for isolated PostgreSQL integration tests"
        )
    return Settings()


@pytest.fixture(scope="module")
def database(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def browser(settings: Settings) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=os.environ["FOXTUBE_TEST_BASE_URL"], timeout=10
    ) as client:
        yield client


def test_readiness_through_nginx(browser: httpx.Client) -> None:
    response = browser.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": dict.fromkeys(("database", "schema", "worker", "storage"), "ok"),
    }
    assert browser.get("/health/live").json() == {"status": "ok"}
    assert browser.get("/").status_code == 200
    assert browser.get("/docs").status_code == 200
    assert (
        browser.get("/static/vendor/swagger-ui/swagger-ui-bundle.js").status_code == 200
    )
    assert "/health/ready" in browser.get("/openapi.json").json()["paths"]


def test_known_media_file_cannot_be_accessed_directly(
    settings: Settings, browser: httpx.Client
) -> None:
    name = f"private-probe-{uuid4().hex}.mp4"
    path = settings.media_root / name
    path.write_bytes(b"private media access probe")
    try:
        for method, headers in (
            ("GET", {}),
            ("HEAD", {}),
            ("GET", {"Range": "bytes=0-6"}),
        ):
            response = browser.request(
                method, f"/internal-media/{name}", headers=headers
            )
            assert response.status_code == 404
            assert b"private media access probe" not in response.content
        assert browser.get(f"/static/{name}").status_code == 404
    finally:
        path.unlink()


def test_security_boundaries_through_nginx(browser: httpx.Client) -> None:
    response = browser.get("/", headers={"Host": "attacker.invalid"})
    assert response.status_code == 400
    query = "<script>alert(1)</script>"
    response = browser.get("/search", params={"q": query})
    assert response.status_code == 200
    assert query not in response.text
    assert "&lt;script&gt;" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("role", ["api", "worker"])
def test_runtime_database_privileges(settings: Settings, role: str) -> None:
    password = os.environ[f"FOXTUBE_TEST_{role.upper()}_DB_PASSWORD"]
    runtime = make_engine(
        settings.model_copy(
            update={"db_user": f"foxtube_{role}", "db_password": SecretStr(password)}
        )
    )
    try:
        with runtime.connect() as connection:
            privileges = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            assert privileges == (False, False, False)
            assert (
                connection.scalar(text("SELECT count(*) FROM service_heartbeats")) >= 1
            )
        forbidden = [
            "CREATE TABLE forbidden_probe (id int)",
            "DELETE FROM service_heartbeats",
            "DELETE FROM videos WHERE false",
            "UPDATE videos SET moderation_status = 'visible' WHERE false",
        ]
        if role == "api":
            forbidden.extend(
                [
                    "INSERT INTO service_heartbeats VALUES ('forbidden', now())",
                    "UPDATE videos SET playback_key = 'forbidden' WHERE false",
                ]
            )
        if role == "worker":
            forbidden.extend(
                [
                    "UPDATE videos SET visibility = 'public' WHERE false",
                    "UPDATE videos SET deleted_at = NULL WHERE false",
                ]
            )
        for statement in forbidden:
            with runtime.begin() as connection:
                with pytest.raises(DBAPIError) as failure:
                    connection.execute(text(statement))
                assert failure.value.orig.sqlstate == "42501"
        if role == "worker":
            # Exercise the worker's actual write grant, then roll the test row back.
            with runtime.connect() as connection:
                connection.execute(
                    text("INSERT INTO service_heartbeats VALUES (:name, now())"),
                    {"name": f"probe-{uuid4().hex[:16]}"},
                )
                connection.rollback()
    finally:
        runtime.dispose()


def test_migrations_round_trip_in_separate_database(settings: Settings) -> None:
    name = f"foxtube_migrations_{uuid4().hex}"
    admin = create_engine(
        settings.database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        environment = dict(os.environ, FOXTUBE_DB_NAME=name)
        for arguments in (
            ("upgrade", "head"),
            ("check",),
            ("downgrade", "base"),
            ("upgrade", "head"),
            ("check",),
        ):
            completed = subprocess.run(
                ["alembic", *arguments],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
