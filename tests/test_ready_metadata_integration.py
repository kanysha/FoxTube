"""READY metadata invariants enforced by the real PostgreSQL constraint."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from test_video_integration import VideoAccounts, delete_video, ready_video
from test_video_integration import (
    independent_video_rate_limits as independent_video_rate_limits,
)
from test_video_integration import sample_video as sample_video
from test_video_integration import video_accounts as video_accounts

from app.videos.models import Video

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ready_metadata_video(
    video_accounts: VideoAccounts, sample_video: bytes
) -> Iterator[dict[str, Any]]:
    video = ready_video(video_accounts, sample_video)
    try:
        yield video
    finally:
        delete_video(video_accounts, video["id"])


@pytest.mark.parametrize(
    "field", ["duration", "width", "height", "playback_key", "poster_key"]
)
def test_ready_video_rejects_missing_metadata(
    video_accounts: VideoAccounts,
    ready_metadata_video: dict[str, Any],
    field: str,
) -> None:
    # The fixture was uploaded through Nginx and processed by the real worker.
    # Each rejected mutation rolls back, preserving the valid READY result.
    with video_accounts.database.connect() as connection:
        with pytest.raises(IntegrityError) as rejected, connection.begin():
            connection.execute(
                update(Video)
                .where(Video.id == UUID(ready_metadata_video["id"]))
                .values({field: None})
            )
        assert isinstance(rejected.value.orig, CheckViolation)
        assert rejected.value.orig.sqlstate == "23514"
        assert rejected.value.orig.diag.constraint_name == "videos_ready_artifacts"
