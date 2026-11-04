import json
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from test_auth_integration import AuthBrowser, expect_status, require, runtime_engine
from test_video_integration import (
    VideoAccounts,
    await_state,
    create_video,
    delete_video,
    media_requests,
    mutation_headers,
    ready_video,
    send_source,
)
from test_video_integration import (
    independent_video_rate_limits as independent_video_rate_limits,
)
from test_video_integration import (
    sample_video as sample_video,
)
from test_video_integration import (
    video_accounts as video_accounts,
)

from app.moderation.bootstrap import BootstrapError, bootstrap_admin
from app.moderation.service import ROLE_LOCK

pytestmark = pytest.mark.integration


@dataclass(repr=False)
class ModerationAccounts:
    videos: VideoAccounts = field(repr=False)
    admin: AuthBrowser = field(repr=False)
    moderator: AuthBrowser = field(repr=False)
    second_admin: AuthBrowser = field(repr=False)
    admin_user: dict[str, Any] = field(repr=False)
    moderator_user: dict[str, Any] = field(repr=False)
    second_admin_user: dict[str, Any] = field(repr=False)


def patch(browser: AuthBrowser, path: str, payload: dict[str, Any]) -> httpx.Response:
    return browser.client.patch(path, json=payload, headers=mutation_headers(browser))


def set_role(browser: AuthBrowser, user_id: str, role: str) -> httpx.Response:
    return patch(
        browser,
        "/api/v1/admin/roles",
        {
            "user_id": user_id,
            "role": role,
            "reason": "Изолированная проверка ролей.",
        },
    )


def moderate(browser: AuthBrowser, video_id: str, status: str) -> httpx.Response:
    return patch(
        browser,
        f"/api/v1/moderation/videos/{video_id}",
        {
            "status": status,
            "reason": "Изолированная проверка модерации.",
        },
    )


def wait_for_function_lock(connection: Connection, function: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        # pg_stat_activity can cache an observation for a transaction.
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        blocked = connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'active' AND wait_event_type = 'Lock' "
                "AND query LIKE :pattern AND pid <> pg_backend_pid()"
            ),
            {"pattern": f"%{function}%"},
        )
        if blocked:
            return
        time.sleep(0.025)
    pytest.fail("Request did not reach the expected database lock", pytrace=False)


@pytest.fixture(scope="module")
def moderation_accounts(video_accounts: VideoAccounts) -> Iterator[ModerationAccounts]:
    clients: list[httpx.Client] = []
    users: list[dict[str, Any]] = []

    def registered(name: str) -> tuple[AuthBrowser, dict[str, Any]]:
        client = httpx.Client(base_url=os.environ["FOXTUBE_TEST_BASE_URL"], timeout=20)
        clients.append(client)
        browser = AuthBrowser(
            client, str(video_accounts.settings.public_origin).rstrip("/")
        )
        response = client.get("/api/v1/auth/session")
        expect_status(response, 200)
        browser.read_session(response)
        user = browser.register(display_name=name)
        users.append(user)
        return browser, user

    try:
        admin, admin_user = registered("Первый администратор проверки")
        moderator, moderator_user = registered("Модератор проверки")
        second_admin, second_admin_user = registered("Резервный администратор проверки")
        assigned = bootstrap_admin(video_accounts.database, admin_user["email"])
        require(
            str(assigned) == admin_user["id"], "Bootstrap promoted the wrong account"
        )
        expect_status(
            patch(
                admin,
                "/api/v1/admin/roles",
                {
                    "email": f"  {moderator_user['email'].upper()}  ",
                    "role": "moderator",
                    "reason": "Назначение по точному нормализованному email.",
                },
            ),
            200,
        )
        yield ModerationAccounts(
            video_accounts,
            admin,
            moderator,
            second_admin,
            admin_user,
            moderator_user,
            second_admin_user,
        )
    finally:
        for client in clients:
            client.close()
        if users:
            with video_accounts.database.begin() as connection:
                connection.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"),
                    {"ids": [UUID(user["id"]) for user in users]},
                )


def test_initial_admin_is_offline_privileged_exact_and_once(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    with pytest.raises(BootstrapError):
        bootstrap_admin(accounts.videos.database, accounts.second_admin_user["email"])
    api = runtime_engine(accounts.videos.settings, "api")
    try:
        with pytest.raises(BootstrapError):
            bootstrap_admin(api, accounts.second_admin_user["email"])
    finally:
        api.dispose()
    with accounts.videos.database.connect() as connection:
        event = connection.execute(
            text(
                "SELECT actor_id, target_user_id, previous_value, new_value "
                "FROM audit_events "
                "WHERE action = 'role.bootstrap'"
            )
        ).one()
    require(
        event.actor_id is None
        and str(event.target_user_id) == accounts.admin_user["id"]
        and event.previous_value == "user"
        and event.new_value == "admin",
        "First administrator setup was not audited atomically",
    )
    expect_status(accounts.admin.client.post("/api/v1/admin/bootstrap", json={}), 404)


def test_report_duplicate_race_real_media_hide_restore_and_resolution(
    moderation_accounts: ModerationAccounts,
    sample_video: bytes,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = ready_video(videos, sample_video, visibility="public")
    video_id = video["id"]
    path = f"/api/v1/videos/{video_id}/reports"
    barrier = Barrier(2)

    def report() -> httpx.Response:
        barrier.wait(timeout=3)
        return videos.grantee.post(path, {"reason": "Повторная конкурентная жалоба."})

    try:
        media_requests(videos.guest, video_id, 200)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: report(), range(2)))
        require(
            sorted(response.status_code for response in responses) == [201, 409],
            "Concurrent duplicate reports were not deduplicated by PostgreSQL",
        )
        report_id = next(
            response.json()["id"]
            for response in responses
            if response.status_code == 201
        )
        detail_path = f"/api/v1/moderation/reports/{report_id}"
        detail = accounts.moderator.client.get(detail_path)
        expect_status(detail, 200)
        require(
            detail.json()["video"]["title"] == video["title"],
            "Readable report title is missing",
        )
        expect_status(moderate(accounts.moderator, video_id, "hidden"), 200)
        for client in (
            videos.owner.client,
            videos.guest,
            accounts.moderator.client,
            accounts.admin.client,
        ):
            media_requests(client, video_id, 404)
        hidden = videos.owner.client.get(f"/api/v1/videos/{video_id}")
        expect_status(hidden, 200)
        require(hidden.json()["playback_url"] is None, "Owner can play a hidden video")
        listed = videos.guest.get("/api/v1/videos", params={"q": video["title"]})
        expect_status(listed, 200)
        require(listed.json()["total"] == 0, "Hidden video remains in discovery counts")
        expect_status(moderate(accounts.moderator, video_id, "visible"), 200)
        media_requests(videos.guest, video_id, 200)
        resolved = accounts.moderator.post(
            f"{detail_path}/resolve", {"resolution": "Проверено. Нарушение устранено."}
        )
        expect_status(resolved, 200)
        require(
            resolved.json()["status"] == "resolved" and resolved.json()["resolution"],
            "Resolution was not persisted",
        )
        expect_status(
            accounts.moderator.post(
                f"{detail_path}/resolve", {"resolution": "Повторное решение."}
            ),
            409,
        )
        reopened = videos.grantee.post(
            path, {"reason": "Новая жалоба после закрытия предыдущей."}
        )
        expect_status(reopened, 201)
        with videos.database.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT action, count(*) FROM audit_events "
                    "WHERE video_id = :id GROUP BY action"
                ),
                {"id": UUID(video_id)},
            ).all()
        require(
            dict(rows)
            == {
                "report.created": 2,
                "report.resolved": 1,
                "video.hidden": 1,
                "video.restored": 1,
            },
            "Audit contains missing or duplicated moderation actions",
        )
        expect_status(accounts.moderator.client.get("/moderation"), 200)
        expect_status(accounts.moderator.client.get("/moderation?closed=true"), 200)
        expect_status(accounts.admin.client.get("/admin"), 200)
    finally:
        delete_video(videos, video_id)


def test_private_report_card_hides_title_personal_data_and_every_media_method(
    moderation_accounts: ModerationAccounts,
    sample_video: bytes,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = ready_video(videos, sample_video)
    video_id = video["id"]
    try:
        expect_status(
            videos.owner.post(
                f"/api/v1/videos/{video_id}/grants",
                {"email": videos.grantee_user["email"]},
            ),
            204,
        )
        response = videos.grantee.post(
            f"/api/v1/videos/{video_id}/reports",
            {"reason": "Проверить приватный материал."},
        )
        expect_status(response, 201)
        report_id = response.json()["id"]
        for browser in (accounts.moderator, accounts.admin):
            detail = browser.client.get(f"/api/v1/moderation/reports/{report_id}")
            expect_status(detail, 200)
            require(
                detail.json()["video"]["watch_url"] is None
                and video["title"] not in detail.text
                and videos.owner_user["email"] not in detail.text
                and videos.grantee_user["email"] not in detail.text
                and "playback_key" not in detail.text
                and "poster_key" not in detail.text,
                "Service card exposes private metadata or personal data",
            )
            media_requests(browser.client, video_id, 404)
            expect_status(browser.client.get(f"/watch/{video_id}"), 404)
            expect_status(
                browser.post(
                    f"/api/v1/videos/{video_id}/reports", {"reason": "Нет доступа."}
                ),
                404,
            )
            page = browser.client.get("/moderation")
            expect_status(page, 200)
            require(
                video["title"] not in page.text,
                "Moderation HTML leaked a private title",
            )
        expect_status(
            videos.owner.client.delete(
                f"/api/v1/videos/{video_id}/grants/{videos.grantee_user['id']}",
                headers=mutation_headers(videos.owner),
            ),
            204,
        )
        expect_status(
            videos.grantee.post(
                f"/api/v1/videos/{video_id}/reports", {"reason": "Доступ отозван."}
            ),
            404,
        )
    finally:
        delete_video(videos, video_id)


def test_concurrent_grant_revocation_is_rechecked_after_report_lock(
    moderation_accounts: ModerationAccounts,
    sample_video: bytes,
) -> None:
    videos = moderation_accounts.videos
    video = ready_video(videos, sample_video)
    video_id = video["id"]
    try:
        expect_status(
            videos.owner.post(
                f"/api/v1/videos/{video_id}/grants",
                {
                    "email": videos.grantee_user["email"],
                },
            ),
            204,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            with videos.database.begin() as connection:
                connection.execute(
                    text("SELECT id FROM videos WHERE id = :id FOR UPDATE"),
                    {"id": UUID(video_id)},
                )
                pending = executor.submit(
                    videos.grantee.post,
                    f"/api/v1/videos/{video_id}/reports",
                    {"reason": "Запрос начат до отзыва доступа."},
                )
                wait_for_function_lock(connection, "foxtube_report_video")
                connection.execute(
                    text(
                        "DELETE FROM video_grants "
                        "WHERE video_id = :video AND user_id = :viewer"
                    ),
                    {
                        "video": UUID(video_id),
                        "viewer": UUID(videos.grantee_user["id"]),
                    },
                )
            expect_status(pending.result(timeout=5), 404)
        with videos.database.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM reports WHERE video_id = :id"),
                {"id": UUID(video_id)},
            )
        require(
            count == 0, "Report accepted a grant revoked while it waited for a lock"
        )
    finally:
        delete_video(videos, video_id)


def test_hidden_before_processing_stays_hidden_and_restore_preserves_privacy(
    moderation_accounts: ModerationAccounts,
    sample_video: bytes,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = create_video(videos, len(sample_video))
    video_id = video["id"]
    try:
        expect_status(moderate(accounts.moderator, video_id, "hidden"), 200)
        expect_status(
            videos.grantee.post(
                f"/api/v1/videos/{video_id}/reports", {"reason": "Ещё не готово."}
            ),
            404,
        )
        expect_status(send_source(videos.owner, video_id, sample_video), 202)
        processed = await_state(videos.owner, video_id)
        require(
            processed["playback_url"] is None,
            "Worker restored playback for a hidden upload",
        )
        with videos.database.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT moderation_status, visibility, status "
                    "FROM videos WHERE id = :id"
                ),
                {"id": UUID(video_id)},
            ).one()
        require(
            tuple(row) == ("hidden", "private", "READY"),
            "Worker changed moderation or privacy",
        )
        media_requests(videos.owner.client, video_id, 404)
        expect_status(moderate(accounts.moderator, video_id, "visible"), 200)
        media_requests(videos.owner.client, video_id, 200)
        media_requests(videos.guest, video_id, 404)
        media_requests(accounts.moderator.client, video_id, 404)
    finally:
        delete_video(videos, video_id)


def test_privilege_boundaries_csrf_origin_and_input_rejection(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    target = str(uuid4())
    expect_status(
        videos.guest.post(
            f"/api/v1/videos/{target}/reports", json={"reason": "Без входа."}
        ),
        401,
    )
    for path in (
        "/api/v1/moderation/reports",
        "/api/v1/admin/audit",
        f"/api/v1/admin/users?email={accounts.admin_user['email']}",
        "/moderation",
        "/admin",
    ):
        expect_status(videos.owner.client.get(path), 403)
    expect_status(accounts.moderator.client.get("/api/v1/admin/audit"), 403)
    expect_status(moderate(videos.owner, target, "hidden"), 403)
    actions = (
        ("POST", f"/api/v1/videos/{target}/reports", {"reason": "Жалоба."}),
        (
            "PATCH",
            f"/api/v1/moderation/videos/{target}",
            {"status": "hidden", "reason": "Скрытие."},
        ),
        (
            "POST",
            f"/api/v1/moderation/reports/{target}/resolve",
            {"resolution": "Решение."},
        ),
        (
            "PATCH",
            "/api/v1/admin/roles",
            {
                "user_id": accounts.moderator_user["id"],
                "role": "admin",
                "reason": "Роль.",
            },
        ),
    )
    for method, path, payload in actions:
        for headers in (
            {"Origin": accounts.admin.origin},
            {
                "Origin": "https://untrusted.example",
                "X-CSRF-Token": accounts.admin.csrf_token,
            },
        ):
            response = accounts.admin.client.request(
                method, path, json=payload, headers=headers
            )
            expect_status(response, 403)
    expect_status(
        patch(
            accounts.admin,
            "/api/v1/admin/roles",
            {
                "user_id": accounts.moderator_user["id"],
                "role": "superadmin",
                "reason": "Нельзя.",
            },
        ),
        422,
    )
    expect_status(
        patch(
            accounts.admin,
            "/api/v1/admin/roles",
            {
                "user_id": accounts.moderator_user["id"],
                "email": accounts.admin_user["email"],
                "role": "admin",
                "reason": "Две цели.",
            },
        ),
        422,
    )
    expect_status(
        patch(
            accounts.admin,
            "/api/v1/admin/roles",
            {
                "email": accounts.moderator_user["email"][1:],
                "role": "admin",
                "reason": "Только точный email.",
            },
        ),
        404,
    )
    expect_status(
        patch(
            accounts.admin,
            "/api/v1/admin/roles",
            {
                "user_id": accounts.moderator_user["id"],
                "role": "user",
                "reason": "   ",
            },
        ),
        422,
    )


@pytest.mark.parametrize(
    "invalid", ["bad\x00text", "bad\ud800text"], ids=["nul", "surrogate"]
)
def test_unrepresentable_text_returns_422_before_moderation_or_role_change(
    moderation_accounts: ModerationAccounts,
    invalid: str,
) -> None:
    accounts = moderation_accounts
    target = str(uuid4())
    actions = (
        ("POST", f"/api/v1/videos/{target}/reports", {"reason": invalid}),
        (
            "POST",
            f"/api/v1/moderation/reports/{target}/resolve",
            {"resolution": invalid},
        ),
        (
            "PATCH",
            f"/api/v1/moderation/videos/{target}",
            {"reason": invalid, "status": "hidden"},
        ),
        (
            "PATCH",
            "/api/v1/admin/roles",
            {
                "user_id": accounts.moderator_user["id"],
                "role": "admin",
                "reason": invalid,
            },
        ),
    )
    for method, path, payload in actions:
        response = accounts.admin.client.request(
            method,
            path,
            content=json.dumps(payload).encode("ascii"),
            headers={
                **mutation_headers(accounts.admin),
                "Content-Type": "application/json",
            },
        )
        expect_status(response, 422)
    current = accounts.moderator.client.get("/api/v1/auth/me")
    expect_status(current, 200)
    require(
        current.json()["role"] == "moderator", "Invalid audit reason changed a role"
    )


def test_last_admin_race_preserves_one_admin_and_audit(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    expect_status(
        set_role(accounts.admin, accounts.second_admin_user["id"], "admin"), 200
    )
    barrier = Barrier(2)

    def demote(pair: tuple[AuthBrowser, str]) -> httpx.Response:
        barrier.wait(timeout=3)
        return set_role(pair[0], pair[1], "user")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    demote,
                    (
                        (accounts.admin, accounts.admin_user["id"]),
                        (accounts.second_admin, accounts.second_admin_user["id"]),
                    ),
                )
            )
        require(
            sorted(response.status_code for response in responses) == [200, 409],
            "Concurrent demotions removed the last administrator",
        )
        with accounts.videos.database.connect() as connection:
            admins = list(
                connection.scalars(text("SELECT id FROM users WHERE role = 'admin'"))
            )
        require(len(admins) == 1, "Last-admin invariant failed in PostgreSQL")
    finally:
        with accounts.videos.database.connect() as connection:
            remaining = connection.scalar(
                text("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
            )
        browser = (
            accounts.admin
            if str(remaining) == accounts.admin_user["id"]
            else accounts.second_admin
        )
        expect_status(set_role(browser, accounts.admin_user["id"], "admin"), 200)
        expect_status(
            set_role(accounts.admin, accounts.second_admin_user["id"], "user"), 200
        )


def test_role_revocation_wins_over_privileged_request_waiting_for_lock(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = create_video(videos, 32)
    video_id = video["id"]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with videos.database.begin() as connection:
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLE_LOCK}
                )
                pending = executor.submit(
                    moderate, accounts.moderator, video_id, "hidden"
                )
                wait_for_function_lock(connection, "foxtube_moderate_video")
                connection.execute(
                    text(
                        "SELECT public.foxtube_set_role("
                        ":actor, :target, 'user', :reason, :audit)"
                    ),
                    {
                        "actor": UUID(accounts.admin_user["id"]),
                        "target": UUID(accounts.moderator_user["id"]),
                        "reason": "Отзыв роли во время ожидающего запроса.",
                        "audit": uuid4(),
                    },
                )
            expect_status(pending.result(timeout=5), 403)
        with videos.database.connect() as connection:
            status = connection.scalar(
                text("SELECT moderation_status FROM videos WHERE id = :id"),
                {"id": UUID(video_id)},
            )
        require(
            status == "visible",
            "A stale moderator request changed video state after demotion",
        )
    finally:
        expect_status(
            set_role(accounts.admin, accounts.moderator_user["id"], "moderator"), 200
        )
        delete_video(videos, video_id)


def test_resolved_report_requires_non_null_resolution_in_postgres(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = create_video(videos, 32)
    try:
        with videos.database.connect() as connection:
            with pytest.raises(DBAPIError) as failure:
                connection.execute(
                    text(
                        "INSERT INTO reports (id, video_id, reporter_id, reason, "
                        "status, resolved_at) VALUES (:id, :video, :reporter, "
                        "'Причина', 'resolved', now())"
                    ),
                    {
                        "id": uuid4(),
                        "video": UUID(video["id"]),
                        "reporter": UUID(accounts.admin_user["id"]),
                    },
                )
            require(
                getattr(failure.value.orig, "sqlstate", None) == "23514",
                "PostgreSQL accepted a closed report without a resolution",
            )
            connection.rollback()
    finally:
        delete_video(videos, video["id"])


def test_runtime_sql_privileges_and_failed_audit_roll_back_video_change(
    moderation_accounts: ModerationAccounts,
) -> None:
    accounts = moderation_accounts
    videos = accounts.videos
    video = create_video(videos, 32)
    api = runtime_engine(videos.settings, "api")
    worker = runtime_engine(videos.settings, "worker")
    try:
        for engine, statement in (
            (api, "UPDATE videos SET moderation_status = 'hidden' WHERE id = :id"),
            (api, "UPDATE users SET role = 'admin' WHERE id = :id"),
            (api, "DELETE FROM audit_events"),
            (api, "UPDATE audit_events SET reason = 'Перезапись'"),
            (worker, "SELECT * FROM reports LIMIT 1"),
            (worker, "SELECT * FROM audit_events LIMIT 1"),
            (
                worker,
                "SELECT public.foxtube_moderate_video("
                ":actor, :id, 'hidden', 'Попытка', :audit)",
            ),
        ):
            with engine.connect() as connection:
                with pytest.raises(DBAPIError) as failure:
                    connection.execute(
                        text(statement),
                        {
                            "id": UUID(video["id"]),
                            "actor": UUID(accounts.admin_user["id"]),
                            "audit": uuid4(),
                        },
                    )
                require(
                    getattr(failure.value.orig, "sqlstate", None) == "42501",
                    "Runtime SQL privileges exceed the assigned role",
                )
                connection.rollback()
        with videos.database.connect() as connection:
            existing_audit = connection.scalar(
                text("SELECT id FROM audit_events LIMIT 1")
            )
        with api.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "SELECT public.foxtube_moderate_video("
                        ":actor, :id, 'hidden', 'Ошибка аудита', :audit)"
                    ),
                    {
                        "actor": UUID(accounts.admin_user["id"]),
                        "id": UUID(video["id"]),
                        "audit": existing_audit,
                    },
                )
            connection.rollback()
        with videos.database.connect() as connection:
            status = connection.scalar(
                text("SELECT moderation_status FROM videos WHERE id = :id"),
                {"id": UUID(video["id"])},
            )
            audits = connection.scalar(
                text("SELECT count(*) FROM audit_events WHERE video_id = :id"),
                {"id": UUID(video["id"])},
            )
        require(
            status == "visible" and audits == 0,
            "Audit failure left an unaudited video mutation",
        )
    finally:
        api.dispose()
        worker.dispose()
        delete_video(videos, video["id"])
