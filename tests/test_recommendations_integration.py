"""Ranking and ACL on real PostgreSQL; media playback is covered by Chrome E2E."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session
from test_auth_integration import expect_status, require
from test_video_integration import VideoAccounts
from test_video_integration import (
    independent_video_rate_limits as independent_video_rate_limits,
)
from test_video_integration import video_accounts as video_accounts

from app.videos.models import Video, VideoGrant

pytestmark = pytest.mark.integration


def insert_video(
    accounts: VideoAccounts, owner: str, title: str, **values: Any
) -> UUID:
    identifier = uuid4()
    with Session(accounts.database) as session, session.begin():
        session.add(
            Video(
                id=identifier,
                owner_id=UUID(owner),
                title=title,
                expected_size=1,
                reservation_expires_at=datetime.now(UTC),
                status="READY",
                visibility="public",
                playback_key=f"recommendation-test/{identifier}.mp4",
                poster_key=f"recommendation-test/{identifier}.jpg",
                duration=10,
                width=320,
                height=180,
                **values,
            )
        )
    return identifier


def test_recommendations_rank_topic_and_filter_before_limit(
    video_accounts: VideoAccounts,
) -> None:
    a = video_accounts
    ids: list[UUID] = []
    try:
        seed = insert_video(a, a.owner_user["id"], "Орбиты Юпитера")
        ids.append(seed)
        topical = insert_video(a, a.outsider_user["id"], "Орбиты Юпитера: наблюдения")
        ids.append(topical)
        unrelated = insert_video(a, a.owner_user["id"], "Рецепт пирога")
        ids.append(unrelated)
        processing = insert_video(a, a.owner_user["id"], "Орбиты Юпитера: обработка")
        ids.append(processing)
        hidden = insert_video(
            a, a.owner_user["id"], "Орбиты Юпитера", moderation_status="hidden"
        )
        ids.append(hidden)
        private = insert_video(a, a.owner_user["id"], "Орбиты Юпитера — приватное")
        ids.append(private)
        deleted = insert_video(
            a, a.owner_user["id"], "Орбиты Юпитера", deleted_at=datetime.now(UTC)
        )
        ids.append(deleted)
        with a.database.begin() as connection:
            connection.execute(
                update(Video).where(Video.id == private).values(visibility="private")
            )
            connection.execute(
                update(Video).where(Video.id == processing).values(status="PROCESSING")
            )
        url = f"/api/v1/videos/{seed}/recommendations"
        response = a.guest.get(url, params={"limit": 1})
        expect_status(response, 200)
        require(
            [row["id"] for row in response.json()["items"]] == [str(topical)],
            "Topic match must survive filtering before limit",
        )
        response = a.guest.get(url, params={"limit": 20})
        visible = {row["id"] for row in response.json()["items"]}
        require(
            visible.isdisjoint(map(str, [seed, hidden, private, deleted, processing])),
            "Recommendations leaked an unavailable/current video",
        )
        require(str(unrelated) in visible, "Accessible channel fallback is missing")
        with a.database.begin() as connection:
            connection.execute(
                VideoGrant.__table__.insert().values(
                    video_id=private, user_id=UUID(a.grantee_user["id"])
                )
            )
        response = a.grantee.client.get(url)
        require(
            str(private) in {v["id"] for v in response.json()["items"]},
            "Granted private recommendation is missing",
        )
        response = a.outsider.client.get(url)
        require(
            str(private) not in {v["id"] for v in response.json()["items"]},
            "A different account saw a private recommendation",
        )
        with a.database.begin() as connection:
            connection.execute(delete(VideoGrant).where(VideoGrant.video_id == private))
        response = a.grantee.client.get(url)
        require(
            str(private) not in {v["id"] for v in response.json()["items"]},
            "Revoked recommendation remained visible",
        )
        for limit in (0, 21):
            expect_status(a.guest.get(url, params={"limit": limit}), 422)
    finally:
        with a.database.begin() as connection:
            connection.execute(delete(Video).where(Video.id.in_(ids)))


def test_recommendations_guard_seed_and_escape_html(
    video_accounts: VideoAccounts,
) -> None:
    a = video_accounts
    ids: list[UUID] = []
    try:
        seed = insert_video(a, a.owner_user["id"], "Наблюдения Сатурна")
        ids.append(seed)
        candidate = insert_video(
            a, a.owner_user["id"], '<img src=x onerror="alert(1)"> Сатурн'
        )
        ids.append(candidate)
        response = a.guest.get(f"/watch/{seed}")
        expect_status(response, 200)
        require(
            '<img src=x onerror="alert(1)">' not in response.text,
            "Recommendation title became executable HTML",
        )
        require(
            "&lt;img src=x onerror=" in response.text,
            "Escaped recommendation is missing",
        )
        url = f"/api/v1/videos/{seed}/recommendations"
        for values in (
            {"visibility": "private"},
            {"visibility": "public", "moderation_status": "hidden"},
            {"moderation_status": "visible", "status": "UPLOADING"},
        ):
            with a.database.begin() as connection:
                connection.execute(
                    update(Video).where(Video.id == seed).values(**values)
                )
            expect_status(a.guest.get(url), 404)
            owner_response = a.owner.client.get(url)
            expect_status(owner_response, 200)
            if values.get("visibility") != "private":
                require(
                    owner_response.json()["items"] == [],
                    "Owner non-playable seed received an automatic queue",
                )
        with a.database.begin() as connection:
            connection.execute(
                update(Video)
                .where(Video.id == seed)
                .values(deleted_at=datetime.now(UTC))
            )
        expect_status(a.owner.client.get(url), 404)
    finally:
        with a.database.begin() as connection:
            connection.execute(delete(Video).where(Video.id.in_(ids)))
