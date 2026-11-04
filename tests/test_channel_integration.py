from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_auth_integration import expect_status, require
from test_video_integration import (
    VideoAccounts,
    delete_video,
    independent_video_rate_limits,  # noqa: F401
    mutation_headers,
    ready_video,
)
from test_video_integration import (
    sample_video as sample_video_fixture,
)
from test_video_integration import (
    video_accounts as video_accounts_fixture,
)

pytestmark = pytest.mark.integration
sample_video = sample_video_fixture
video_accounts = video_accounts_fixture


def test_channel_filters_acl_before_count_and_pagination(
    video_accounts: VideoAccounts, sample_video: bytes
) -> None:
    a = video_accounts
    public = ready_video(a, sample_video, visibility="public")
    private = ready_video(a, sample_video)
    hidden = ready_video(a, sample_video, visibility="public")
    channel_id = public["channel_id"]
    try:
        with a.database.begin() as connection:
            connection.execute(
                text("UPDATE videos SET moderation_status = 'hidden' WHERE id = :id"),
                {"id": hidden["id"]},
            )
        response = a.owner.post(
            f"/api/v1/videos/{private['id']}/grants", {"email": a.grantee_user["email"]}
        )
        expect_status(response, 204)
        for client, count in (
            (a.guest, 1),
            (a.outsider.client, 1),
            (a.grantee.client, 2),
            (a.owner.client, 2),
        ):
            response = client.get(f"/api/v1/channels/{channel_id}")
            expect_status(response, 200)
            info: dict[str, Any] = response.json()
            require(
                info["videos_count"] == count,
                "Channel count exposes an unreadable video",
            )
            require(
                "email" not in info and "user_id" not in info,
                "Channel leaks account details",
            )
            response = client.get(f"/api/v1/channels/{channel_id}/videos?page_size=1")
            expect_status(response, 200)
            data = response.json()
            require(
                data["total"] == count and len(data["items"]) == 1,
                "ACL must precede channel pagination",
            )
            response = client.get(f"/channels/{channel_id}")
            expect_status(response, 200)
            require(
                public["title"] in response.text,
                "Published video is missing from its channel",
            )
            require(
                hidden["title"] not in response.text,
                "Hidden title leaked through channel HTML",
            )
            if count == 1:
                require(
                    private["title"] not in response.text,
                    "Private title leaked through channel HTML",
                )
        response = a.guest.get(f"/@{a.owner_user['channel_handle']}")
        expect_status(response, 303)
        require(
            response.headers["location"] == f"/channels/{channel_id}",
            "Handle does not resolve to its channel",
        )
        # Removing a grant immediately changes both the counter and list.
        response = a.owner.client.delete(
            f"/api/v1/videos/{private['id']}/grants/{a.grantee_user['id']}",
            headers=mutation_headers(a.owner),
        )
        expect_status(response, 204)
        response = a.grantee.client.get(f"/api/v1/channels/{channel_id}/videos")
        expect_status(response, 200)
        require(response.json()["total"] == 1, "Revoked video remains in the channel")
    finally:
        for video in (public, private, hidden):
            delete_video(a, video["id"])


def test_unknown_channel_and_pagination_are_rejected(
    video_accounts: VideoAccounts,
) -> None:
    client = video_accounts.guest
    for path in (
        f"/api/v1/channels/{uuid4()}",
        f"/channels/{uuid4()}",
        "/@unknown-handle",
    ):
        expect_status(client.get(path), 404)
    with video_accounts.database.connect() as connection:
        channel_id = connection.scalar(
            text("SELECT id FROM channels WHERE user_id = :id"),
            {"id": video_accounts.owner_user["id"]},
        )
    expect_status(client.get(f"/api/v1/channels/{channel_id}/videos?page_size=51"), 422)
    expect_status(client.get(f"/channels/{channel_id}?page=0"), 422)
