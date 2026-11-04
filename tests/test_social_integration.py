import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from test_auth_integration import AuthBrowser, expect_status, require
from test_video_integration import (
    VideoAccounts,
    delete_video,
    mutation_headers,
    ready_video,
)
from test_video_integration import sample_video as sample_video
from test_video_integration import video_accounts as video_accounts

from app.auth.rate_limits import rate_key

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def social_videos(
    video_accounts: VideoAccounts, sample_video: bytes
) -> Iterator[dict[str, dict[str, Any]]]:
    private = ready_video(video_accounts, sample_video)
    public = ready_video(video_accounts, sample_video, visibility="public")
    try:
        yield {"private": private, "public": public}
    finally:
        delete_video(video_accounts, private["id"])
        delete_video(video_accounts, public["id"])


@pytest.fixture(autouse=True)
def independent_social_state(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> Iterator[None]:
    ids = [UUID(item["id"]) for item in social_videos.values()]
    users = [
        UUID(user["id"])
        for user in (
            video_accounts.owner_user,
            video_accounts.grantee_user,
            video_accounts.outsider_user,
        )
    ]

    def clean() -> None:
        with video_accounts.database.begin() as connection:
            for table in ("likes", "comments", "video_grants"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE video_id = ANY(:ids)"),
                    {"ids": ids},
                )
            connection.execute(
                text("DELETE FROM playlists WHERE owner_id = ANY(:ids)"), {"ids": users}
            )
            connection.execute(
                text("DELETE FROM subscriptions WHERE user_id = ANY(:ids)"),
                {"ids": users},
            )
            for visibility, video in social_videos.items():
                connection.execute(
                    text(
                        "UPDATE videos SET visibility = :visibility, "
                        "moderation_status = 'visible', deleted_at = NULL "
                        "WHERE id = :id"
                    ),
                    {"visibility": visibility, "id": UUID(video["id"])},
                )
            connection.execute(
                text("DELETE FROM auth_rate_limits WHERE NOT (key = ANY(:baseline))"),
                {"baseline": list(video_accounts.rate_baseline)},
            )

    clean()
    try:
        yield
    finally:
        clean()


def channel_id(accounts: VideoAccounts, user: dict[str, Any]) -> str:
    with accounts.database.connect() as connection:
        result = connection.scalar(
            text("SELECT id FROM channels WHERE user_id = :id"),
            {"id": UUID(user["id"])},
        )
    require(result is not None, "Registered account has no channel")
    return str(result)


def create_playlist(browser: AuthBrowser, **changes: Any) -> dict[str, Any]:
    data = {"title": "Плейлист проверки", "description": "", "visibility": "private"}
    data.update(changes)
    response = browser.post("/api/v1/playlists", data)
    expect_status(response, 201)
    return dict(response.json())


def grant(accounts: VideoAccounts, video_id: str) -> None:
    expect_status(
        accounts.owner.post(
            f"/api/v1/videos/{video_id}/grants",
            {"email": accounts.grantee_user["email"]},
        ),
        204,
    )


def revoke(accounts: VideoAccounts, video_id: str) -> None:
    expect_status(
        accounts.owner.client.delete(
            f"/api/v1/videos/{video_id}/grants/{accounts.grantee_user['id']}",
            headers=mutation_headers(accounts.owner),
        ),
        204,
    )


def concurrent_put(browser: AuthBrowser, path: str) -> list[int]:
    # Independent HTTP connections exercise both API processes and PostgreSQL.
    def request() -> int:
        with httpx.Client(base_url=browser.client.base_url, timeout=15) as client:
            response = client.put(
                path,
                headers={
                    **mutation_headers(browser),
                    "Cookie": f"foxtube_session={browser.cookie()}",
                },
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=6) as executor:
        return list(executor.map(lambda _: request(), range(6)))


def test_like_and_comments_persist_and_repeated_like_is_atomic(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    video_id = social_videos["public"]["id"]
    path = f"/api/v1/videos/{video_id}"
    browser = video_accounts.grantee
    require(
        concurrent_put(browser, f"{path}/like") == [204] * 6,
        "Concurrent idempotent likes failed",
    )
    comment = browser.post(f"{path}/comments", {"body": "  Настоящий комментарий  "})
    expect_status(comment, 201)
    require(
        comment.json()["body"] == "Настоящий комментарий", "Comment was not normalized"
    )
    with video_accounts.database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM likes WHERE video_id = :id), "
                "(SELECT count(*) FROM comments WHERE video_id = :id)"
            ),
            {"id": UUID(video_id)},
        ).one()
    require(tuple(row) == (1, 1), "Interaction was duplicated or not persisted")
    stats = browser.client.get(f"{path}/social")
    expect_status(stats, 200)
    require(
        stats.json() == {"likes_count": 1, "comments_count": 1, "liked": True},
        "Persisted interaction state is incorrect",
    )
    guest_stats = video_accounts.guest.get(f"{path}/social")
    require(not guest_stats.json()["liked"], "Anonymous visitor inherited another like")
    for _ in range(2):
        expect_status(
            browser.client.delete(f"{path}/like", headers=mutation_headers(browser)),
            204,
        )
    require(
        browser.client.get(f"{path}/social").json()["likes_count"] == 0, "Unlike failed"
    )


def test_comment_delete_only_author_or_video_owner_and_no_private_id_oracle(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    public = social_videos["public"]["id"]
    private = social_videos["private"]["id"]
    body = "<img src=x onerror=alert(1)> & текст"
    response = video_accounts.grantee.post(
        f"/api/v1/videos/{public}/comments", {"body": body}
    )
    expect_status(response, 201)
    comment_id = response.json()["id"]
    require(response.json()["body"] == body, "API altered plain comment text")
    page = video_accounts.outsider.client.get(
        f"/api/v1/videos/{public}/comments"
    ).json()
    require(not page["items"][0]["can_delete"], "Foreign reader can delete a comment")
    expect_status(
        video_accounts.outsider.client.delete(
            f"/api/v1/comments/{comment_id}",
            headers=mutation_headers(video_accounts.outsider),
        ),
        404,
    )
    expect_status(
        video_accounts.owner.client.delete(
            f"/api/v1/comments/{comment_id}",
            headers=mutation_headers(video_accounts.owner),
        ),
        204,
    )
    own = video_accounts.grantee.post(
        f"/api/v1/videos/{public}/comments", {"body": "Мой"}
    )
    expect_status(
        video_accounts.grantee.client.delete(
            f"/api/v1/comments/{own.json()['id']}",
            headers=mutation_headers(video_accounts.grantee),
        ),
        204,
    )
    hidden = video_accounts.owner.post(
        f"/api/v1/videos/{private}/comments", {"body": "Закрытый"}
    )
    existing = video_accounts.outsider.client.delete(
        f"/api/v1/comments/{hidden.json()['id']}",
        headers=mutation_headers(video_accounts.outsider),
    )
    absent = video_accounts.outsider.client.delete(
        f"/api/v1/comments/{uuid4()}", headers=mutation_headers(video_accounts.outsider)
    )
    expect_status(existing, 404)
    expect_status(absent, 404)
    require(
        existing.json() == absent.json(), "Private comment existence is distinguishable"
    )


def test_revocation_blocks_social_reads_writes_and_waiting_like(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    video_id = social_videos["private"]["id"]
    grant(video_accounts, video_id)
    path = f"/api/v1/videos/{video_id}"
    browser = video_accounts.grantee
    # Simulate a revocation holding the same Video lock as the actual service.
    # The like starts before grant deletion commits and waits for that lock.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with video_accounts.database.begin() as connection:
            connection.execute(
                text("SELECT id FROM videos WHERE id = :id FOR UPDATE"),
                {"id": UUID(video_id)},
            )
            future = executor.submit(
                browser.client.put, f"{path}/like", headers=mutation_headers(browser)
            )
            time.sleep(0.3)
            require(not future.done(), "Test like did not wait for the video lock")
            connection.execute(
                text(
                    "DELETE FROM video_grants WHERE video_id = :id AND user_id = :user"
                ),
                {"id": UUID(video_id), "user": UUID(video_accounts.grantee_user["id"])},
            )
        expect_status(future.result(timeout=10), 404)
    for suffix in ("/social", "/comments"):
        expect_status(browser.client.get(path + suffix), 404)
    expect_status(browser.post(f"{path}/comments", {"body": "После отзыва"}), 404)
    expect_status(
        browser.client.put(f"{path}/like", headers=mutation_headers(browser)), 404
    )
    with video_accounts.database.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM likes WHERE video_id = :id"),
            {"id": UUID(video_id)},
        )
    require(count == 0, "An old grant snapshot authorized the waiting like")


@pytest.mark.parametrize("state", ["hidden", "deleted", "processing"])
def test_unavailable_video_rejects_social_for_owner_too(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]], state: str
) -> None:
    video_id = social_videos["public"]["id"]
    changes = {
        "hidden": "moderation_status = 'hidden'",
        "deleted": "deleted_at = now()",
        "processing": "status = 'PROCESSING'",
    }
    with video_accounts.database.begin() as connection:
        connection.execute(
            text(f"UPDATE videos SET {changes[state]} WHERE id = :id"),
            {"id": UUID(video_id)},
        )
    try:
        for browser in (video_accounts.owner, video_accounts.grantee):
            for suffix in ("social", "comments"):
                expect_status(
                    browser.client.get(f"/api/v1/videos/{video_id}/{suffix}"), 404
                )
            expect_status(
                browser.client.put(
                    f"/api/v1/videos/{video_id}/like", headers=mutation_headers(browser)
                ),
                404,
            )
            expect_status(
                browser.post(
                    f"/api/v1/videos/{video_id}/comments", {"body": "Недоступно"}
                ),
                404,
            )
    finally:
        with video_accounts.database.begin() as connection:
            connection.execute(
                text("UPDATE videos SET status = 'READY' WHERE id = :id"),
                {"id": UUID(video_id)},
            )


def test_subscriptions_persist_are_idempotent_and_feed_respects_grants(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    channel = channel_id(video_accounts, video_accounts.owner_user)
    path = f"/api/v1/channels/{channel}/subscription"
    browser = video_accounts.grantee
    require(
        concurrent_put(browser, path) == [204] * 6, "Concurrent subscriptions failed"
    )
    state = browser.client.get(path).json()
    require(
        state == {"subscribers_count": 1, "subscribed": True, "can_subscribe": True},
        "Incorrect subscription state",
    )
    channels = browser.client.get("/api/v1/subscriptions/channels").json()
    require(
        channels["total"] == 1 and channels["items"][0]["id"] == channel,
        "Subscribed channel was not persisted",
    )
    feed = browser.client.get("/api/v1/subscriptions").json()
    require(
        feed["total"] == 1 and feed["items"][0]["id"] == social_videos["public"]["id"],
        "Feed exposed private video",
    )
    grant(video_accounts, social_videos["private"]["id"])
    require(
        browser.client.get("/api/v1/subscriptions").json()["total"] == 2,
        "Granted video is missing from subscriptions",
    )
    revoke(video_accounts, social_videos["private"]["id"])
    require(
        browser.client.get("/api/v1/subscriptions").json()["total"] == 1,
        "Revoked video remained in feed count",
    )
    expect_status(
        video_accounts.owner.client.put(
            path, headers=mutation_headers(video_accounts.owner)
        ),
        422,
    )
    for _ in range(2):
        expect_status(
            browser.client.delete(path, headers=mutation_headers(browser)), 204
        )
    require(
        browser.client.get("/api/v1/subscriptions").json()["total"] == 0,
        "Unsubscribe did not update feed",
    )
    expect_status(video_accounts.owner.client.get("/subscriptions"), 200)


def test_playlist_privacy_owner_rules_acl_counts_and_pagination(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    browser = video_accounts.grantee
    private_id = social_videos["private"]["id"]
    public_id = social_videos["public"]["id"]
    playlist = create_playlist(browser, title="Приватное название не должно утечь")
    path = f"/api/v1/playlists/{playlist['id']}"
    channel = channel_id(video_accounts, video_accounts.grantee_user)
    for client in (video_accounts.guest, video_accounts.outsider.client):
        expect_status(client.get(path), 404)
        expect_status(client.get(f"/playlists/{playlist['id']}"), 404)
        public = client.get(f"/api/v1/channels/{channel}/playlists").json()
        require(
            public["total"] == 0 and public["items"] == [],
            "Private playlist metadata leaked",
        )
    expect_status(
        video_accounts.outsider.client.patch(
            path,
            json={"title": "Чужое"},
            headers=mutation_headers(video_accounts.outsider),
        ),
        404,
    )
    expect_status(
        video_accounts.outsider.client.delete(
            path, headers=mutation_headers(video_accounts.outsider)
        ),
        404,
    )
    expect_status(
        browser.client.put(
            f"{path}/items/{private_id}", headers=mutation_headers(browser)
        ),
        404,
    )
    grant(video_accounts, private_id)
    for video_id in (private_id, public_id, public_id):
        expect_status(
            browser.client.put(
                f"{path}/items/{video_id}", headers=mutation_headers(browser)
            ),
            204,
        )
    updated = browser.client.patch(
        path,
        json={"title": "Общий список", "visibility": "public"},
        headers=mutation_headers(browser),
    )
    expect_status(updated, 200)
    require(
        updated.json()["videos_count"] == 2, "Playlist duplicate add created extra item"
    )
    # Private item was inserted first: page size 1 proves filtering precedes offset.
    guest = video_accounts.guest.get(path, params={"page_size": 1}).json()
    require(
        guest["videos"]["total"] == 1 and guest["playlist"]["videos_count"] == 1,
        "Playlist exposes private item count",
    )
    require(
        guest["videos"]["items"][0]["id"] == public_id,
        "ACL was applied after playlist pagination",
    )
    require(
        social_videos["private"]["title"] not in str(guest),
        "Private title leaked from playlist",
    )
    revoke(video_accounts, private_id)
    own = browser.client.get(path).json()
    require(
        own["videos"]["total"] == 1 and own["playlist"]["videos_count"] == 1,
        "Playlist ownership bypasses video ACL",
    )
    channel_cards = video_accounts.guest.get(
        f"/api/v1/channels/{channel}/playlists"
    ).json()
    require(
        channel_cards["items"][0]["videos_count"] == 1,
        "Playlist card counts unavailable items",
    )
    for client in (browser.client, video_accounts.guest):
        page = client.get(f"/playlists/{playlist['id']}")
        expect_status(page, 200)
        require(
            social_videos["public"]["title"] in page.text,
            "Nonempty playlist HTML did not render its video",
        )
        require(
            social_videos["private"]["title"] not in page.text,
            "Playlist HTML leaked inaccessible title",
        )
    expect_status(browser.client.get("/playlists"), 200)
    expect_status(
        browser.client.delete(
            f"{path}/items/{public_id}", headers=mutation_headers(browser)
        ),
        204,
    )
    require(
        browser.client.get(path).json()["videos"]["total"] == 0,
        "Playlist removal did not persist",
    )
    expect_status(browser.client.delete(path, headers=mutation_headers(browser)), 204)
    expect_status(browser.client.get(path), 404)


def test_social_mutations_require_session_csrf_origin_and_forbid_mass_assignment(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    video_id = social_videos["public"]["id"]
    channel = channel_id(video_accounts, video_accounts.owner_user)
    browser = video_accounts.grantee
    playlist = create_playlist(browser)
    operations = [
        ("PUT", f"/api/v1/videos/{video_id}/like", None),
        ("POST", f"/api/v1/videos/{video_id}/comments", {"body": "Test"}),
        ("PUT", f"/api/v1/channels/{channel}/subscription", None),
        ("POST", "/api/v1/playlists", {"title": "Test"}),
        ("PUT", f"/api/v1/playlists/{playlist['id']}/items/{video_id}", None),
    ]
    for method, path, payload in operations:
        expect_status(video_accounts.guest.request(method, path, json=payload), 401)
        expect_status(browser.client.request(method, path, json=payload), 403)
        headers = {**mutation_headers(browser), "Origin": "https://other.example"}
        expect_status(
            browser.client.request(method, path, json=payload, headers=headers), 403
        )
    for payload in (
        {"body": " "},
        {"body": "x" * 2001},
        {"body": "Null\x00 character"},
        {"body": "ok", "user_id": video_accounts.owner_user["id"]},
    ):
        expect_status(browser.post(f"/api/v1/videos/{video_id}/comments", payload), 422)
    encoded_surrogate = json.dumps(
        {"body": "Unpaired \ud800 surrogate"}, ensure_ascii=True
    ).encode("ascii")
    expect_status(
        browser.client.post(
            f"/api/v1/videos/{video_id}/comments",
            content=encoded_surrogate,
            headers={**mutation_headers(browser), "Content-Type": "application/json"},
        ),
        422,
    )
    for payload in (
        {"title": " "},
        {"title": "x" * 201},
        {"title": "ok", "owner_id": video_accounts.owner_user["id"]},
        {"title": "ok", "visibility": "unlisted"},
    ):
        expect_status(browser.post("/api/v1/playlists", payload), 422)
    expect_status(video_accounts.guest.get("/api/v1/subscriptions"), 401)
    expect_status(video_accounts.guest.get("/api/v1/playlists"), 401)


def test_comment_rate_limit_is_shared_and_rejected_attempt_is_persisted(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    browser = video_accounts.grantee
    key = rate_key("social-comment-user", video_accounts.grantee_user["id"])
    with video_accounts.database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO auth_rate_limits (key, attempts, expires_at) "
                "VALUES (:key, 30, now() + interval '15 minutes')"
            ),
            {"key": key},
        )
    response = browser.post(
        f"/api/v1/videos/{social_videos['public']['id']}/comments", {"body": "Лимит"}
    )
    expect_status(response, 429)
    require(
        response.headers.get("retry-after", "").isdigit(),
        "Rate limit has no Retry-After",
    )
    with video_accounts.database.connect() as connection:
        attempts = connection.scalar(
            text("SELECT attempts FROM auth_rate_limits WHERE key = :key"), {"key": key}
        )
    require(
        attempts == 31, "Rejected social action was not committed to the shared limit"
    )


def test_nonempty_playlist_page_escapes_user_metadata(
    video_accounts: VideoAccounts, social_videos: dict[str, dict[str, Any]]
) -> None:
    title = '<script>alert("playlist")</script>'
    playlist = create_playlist(video_accounts.grantee, title=title, visibility="public")
    path = f"/api/v1/playlists/{playlist['id']}"
    expect_status(
        video_accounts.grantee.client.put(
            f"{path}/items/{social_videos['public']['id']}",
            headers=mutation_headers(video_accounts.grantee),
        ),
        204,
    )
    page = video_accounts.guest.get(f"/playlists/{playlist['id']}")
    expect_status(page, 200)
    require(
        "<script>alert(" not in page.text and "&lt;script&gt;" in page.text,
        "Playlist metadata is not HTML-escaped",
    )
