import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from test_auth_integration import require

from app.core.config import Settings
from app.core.db import make_engine

pytestmark = pytest.mark.integration


def test_demo_uses_actual_pipeline_and_repeat_preserves_data(tmp_path: Path) -> None:
    if not os.environ.get("FOXTUBE_TEST_BASE_URL"):
        pytest.fail("Run this test via scripts/test.sh on disposable data")
    engine = make_engine(Settings())
    path = tmp_path / "demo.json"
    ids: list[UUID] = []
    try:
        # This test project is disposable and scenarios run sequentially.
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM auth_rate_limits"))
            original_users = connection.scalar(text("SELECT count(*) FROM users"))
        counts: dict[str, int] | None = None
        for _ in range(2):
            result = subprocess.run(
                ["python", "-m", "app.demo", "--manifest", str(path)],
                capture_output=True,
                timeout=300,
                check=False,
            )
            require(result.returncode == 0, "Actual demo creation/retry failed")
            data: dict[str, Any] = json.loads(path.read_text())
            emails = [account["email"] for account in data["accounts"]]
            require(
                path.stat().st_mode & 0o777 == 0o600, "Demo credentials are not private"
            )
            with engine.connect() as connection:
                ids = list(
                    connection.scalars(
                        text("SELECT id FROM users WHERE email = ANY(:emails)"),
                        {"emails": emails},
                    )
                )
                current = {
                    "users": len(ids),
                    "videos": int(
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM videos "
                                "WHERE owner_id = ANY(:ids) AND status = 'READY' "
                                "AND playback_key IS NOT NULL "
                                "AND poster_key IS NOT NULL"
                            ),
                            {"ids": ids},
                        )
                        or 0
                    ),
                    "comments": int(
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM comments "
                                "WHERE user_id = ANY(:ids)"
                            ),
                            {"ids": ids},
                        )
                        or 0
                    ),
                    "playlists": int(
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM playlists "
                                "WHERE owner_id = ANY(:ids)"
                            ),
                            {"ids": ids},
                        )
                        or 0
                    ),
                }
                require(
                    connection.scalar(
                        text("SELECT count(*) FROM users WHERE NOT (id = ANY(:ids))"),
                        {"ids": ids},
                    )
                    == original_users,
                    "Demo changed unrelated accounts",
                )
                require(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM sessions WHERE user_id = ANY(:ids) "
                            "AND revoked_at IS NULL AND expires_at > now()"
                        ),
                        {"ids": ids},
                    )
                    == 0,
                    "Demo left authenticated sessions active after completion",
                )
            require(
                current == {"users": 6, "videos": 12, "comments": 6, "playlists": 6},
                "Demo did not persist its actual records",
            )
            if counts is not None:
                require(current == counts, "Repeated demo creation duplicated records")
            counts = current
        # A subsequent seed must respect manual moderation/deletion instead of
        # recreating a comment whose ID is already recorded in the manifest.
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM comments WHERE id = :id"),
                {"id": UUID(data["accounts"][0]["comment_id"])},
            )
        result = subprocess.run(
            ["python", "-m", "app.demo", "--manifest", str(path)],
            capture_output=True,
            timeout=300,
            check=False,
        )
        require(result.returncode == 0, "Demo retry after manual deletion failed")
        with engine.connect() as connection:
            require(
                connection.scalar(
                    text("SELECT count(*) FROM comments WHERE user_id = ANY(:ids)"),
                    {"ids": ids},
                )
                == 5,
                "Demo recreated a deliberately deleted comment",
            )
    finally:
        if ids:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids}
                )
                connection.execute(text("DELETE FROM auth_rate_limits"))
        engine.dispose()
