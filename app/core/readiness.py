import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.models import ServiceHeartbeat

WORKER_NAME = "primary"
HEARTBEAT_INTERVAL = 5
HEARTBEAT_TTL = 20
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def expected_heads() -> set[str]:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


def database_checks(engine: Engine) -> dict[str, str]:
    checks = {
        "database": "unavailable",
        "schema": "unavailable",
        "worker": "unavailable",
    }
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
            checks["database"] = "ok"
            heads = set(MigrationContext.configure(connection).get_current_heads())
            if not heads or heads != expected_heads():
                return checks
            checks["schema"] = "ok"
            alive = connection.scalar(
                select(ServiceHeartbeat.name).where(
                    ServiceHeartbeat.name == WORKER_NAME,
                    ServiceHeartbeat.last_seen
                    >= func.now() - timedelta(seconds=HEARTBEAT_TTL),
                )
            )
            checks["worker"] = "ok" if alive else "unavailable"
    except SQLAlchemyError:
        # Public probes never disclose connection strings or database exceptions.
        pass
    return checks


def storage_available(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
