"""Finish the disposable Chrome scenario with account and content lifecycle UI.

Call check_lifecycle last: it removes the supplied video's like/comment, edits and
deletes its playlist, then deletes the video. Other videos remain untouched.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Browser, BrowserContext, Page, expect
from privacy import capture, check_media

TEST_COMMENT = "Проверка сохранения: <script>window.commentExecuted=true</script>"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_json(context: BrowserContext, url: str) -> dict[str, Any]:
    response = context.request.get(url)
    try:
        require(response.status == 200, "Lifecycle object could not be read")
        return response.json()
    finally:
        response.dispose()


def expect_status(context: BrowserContext, url: str, expected: int) -> None:
    response = context.request.get(url)
    try:
        require(response.status == expected, "Lifecycle object has unexpected access")
    finally:
        response.dispose()


def mobile(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        """() => {
          const sidebar = document.querySelector('#site-sidebar');
          return document.body.dataset.sidebarOpen === 'false' &&
            sidebar.getBoundingClientRect().right <= 1;
        }"""
    )
    require(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        ),
        "Lifecycle controls overflow on mobile",
    )


def check_lifecycle(
    browser: Browser,
    owner_page: Page,
    base_url: str,
    video_id: str,
    owner_email: str,
    owner_password: str,
    playlist_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    """Check real UI mutations for the ordinary owner from run.py.

    Preconditions: owner_page is signed in as owner_email/owner_password; video_id
    is the owner's READY public video, with one owner like and only TEST_COMMENT
    from check_social; playlist_path is the owner's public playlist containing
    that video. It may contain other videos. Run after privacy/Swagger, before
    final JS-error checks.

    Credentials stay in memory. Failure exceptions contain only stage/type; a
    separate safe lifecycle-report.json preserves progress if no dict is returned.
    """
    report: dict[str, Any] = {
        "status": "failed",
        "phase": "preconditions",
        "video_id": video_id,
    }
    guest: BrowserContext | None = None
    wrong_login: Page | None = None
    try:
        owner_page.set_viewport_size({"width": 1440, "height": 900})
        api_path = f"/api/v1/videos/{video_id}"
        watch_path = f"/watch/{video_id}"
        playlist_api = f"/api/v1{playlist_path}"
        identity = read_json(owner_page.context, f"{base_url}/api/v1/auth/me")
        require(
            identity["email"] == owner_email and identity["role"] == "user",
            "Lifecycle requires the ordinary video's owner",
        )
        original = read_json(owner_page.context, base_url + api_path)
        require(
            original["can_manage"]
            and original["status"] == "READY"
            and original["visibility"] == "public",
            "Lifecycle requires the owner's READY public video",
        )
        guest = browser.new_context(locale="ru-RU")

        report["phase"] = "logout"
        owner_page.get_by_role("button", name="Выйти из аккаунта", exact=True).click()
        owner_page.wait_for_url(base_url + "/")
        expect_status(owner_page.context, f"{base_url}/api/v1/auth/me", 401)
        owner_page.goto(base_url + "/account")
        owner_page.wait_for_url(base_url + "/login")
        expect(owner_page.locator('[data-auth-form="logout"]')).to_have_count(0)

        report["phase"] = "invalid_password"
        # Chrome reports the expected 401 as a console resource error. Keep this
        # negative request off the owner's globally monitored page, without
        # suppressing any of that page's unexpected console errors.
        wrong_login = guest.new_page()
        wrong_login.set_default_timeout(15000)
        wrong_login.on("pageerror", lambda error: errors.append(type(error).__name__))
        wrong_login.goto(base_url + "/login")
        wrong_password = f"Wrong-lifecycle-password-{uuid4().hex}"
        require(wrong_password != owner_password, "Negative password must differ")
        wrong_login.get_by_label("Email", exact=True).fill(owner_email)
        wrong_login.get_by_label("Пароль", exact=True).fill(wrong_password)
        with wrong_login.expect_response(
            lambda response: (
                response.request.method == "POST"
                and urlsplit(response.url).path == "/api/v1/auth/login"
            )
        ) as rejected:
            wrong_login.get_by_role("button", name="Войти", exact=True).click()
        require(rejected.value.status == 401, "Wrong password was not rejected")
        expect(wrong_login.locator("[data-auth-error]")).to_have_text(
            "Неверный email или пароль. Проверьте данные и попробуйте ещё раз."
        )
        response_text = rejected.value.text()
        visible_text = wrong_login.locator("body").inner_text()
        require(
            all(
                password not in text
                for password in (wrong_password, owner_password)
                for text in (response_text, visible_text)
            ),
            "Authentication error reflected a password",
        )
        expect_status(guest, f"{base_url}/api/v1/auth/me", 401)
        require(
            urlsplit(wrong_login.url).path == "/login",
            "Wrong password left the login screen",
        )
        wrong_login.close()
        wrong_login = None

        report["phase"] = "login_and_persistence"
        owner_page.get_by_label("Email", exact=True).fill(owner_email)
        owner_page.get_by_label("Пароль", exact=True).fill(owner_password)
        owner_page.get_by_role("button", name="Войти", exact=True).click()
        owner_page.wait_for_url(base_url + "/account")
        identity = read_json(owner_page.context, f"{base_url}/api/v1/auth/me")
        require(identity["email"] == owner_email, "Login changed the account")
        owner_page.get_by_role("link", name="Мои видео", exact=True).last.click()
        owner_page.wait_for_url(base_url + "/studio")
        card = owner_page.locator(f'[data-studio-video="{video_id}"]')
        expect(card).to_have_attribute("data-video-status", "READY")
        expect(card).to_contain_text(original["title"])
        card.locator(".manage-video-link").click()
        owner_page.wait_for_url(base_url + watch_path)
        expect(owner_page.get_by_role("heading", level=1)).to_have_text(
            original["title"]
        )
        expect(owner_page.locator(".video-description")).to_have_text(
            original["description"]
        )
        expect(owner_page.locator("[data-like]")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(owner_page.locator("[data-comments-list]")).to_contain_text(TEST_COMMENT)
        playlist = read_json(owner_page.context, base_url + playlist_api)
        playlist_ids = {item["id"] for item in playlist["videos"]["items"]}
        require(video_id in playlist_ids, "Saved video did not survive login")
        report["authentication"] = (
            "ordinary UI logout/login; wrong password 401 with generic error and "
            "no reflected password; previous video and social state preserved"
        )

        title = f"Изменённое видео Chrome {uuid4().hex[:10]}"
        description = "Название и описание сохранены через настройки владельца."

        def edit(visibility: str) -> None:
            form = owner_page.locator("[data-video-edit]")
            form.get_by_label("Название", exact=True).fill(title)
            form.get_by_label("Описание", exact=True).fill(description)
            form.get_by_label("Кто может смотреть", exact=True).select_option(
                visibility
            )
            with owner_page.expect_navigation(wait_until="load"):
                form.get_by_role("button", name="Сохранить", exact=True).click()
            owner_page.reload()
            expect(owner_page.get_by_role("heading", level=1)).to_have_text(title)
            expect(owner_page.locator(".video-description")).to_have_text(description)
            expect(form.locator('[name="title"]')).to_have_value(title)
            expect(form.locator('[name="description"]')).to_have_value(description)
            expect(form.locator('[name="visibility"]')).to_have_value(visibility)
            metadata = read_json(owner_page.context, base_url + api_path)
            require(
                metadata["title"] == title
                and metadata["description"] == description
                and metadata["visibility"] == visibility,
                "Video edit was not persisted",
            )

        report["phase"] = "edit_private"
        edit("private")
        expect(owner_page.locator(".privacy-badge")).to_have_text("Приватное")
        check_media(owner_page.context, base_url, video_id, allowed=True)
        check_media(guest, base_url, video_id, allowed=False)
        report["phase"] = "edit_public"
        edit("public")
        expect(owner_page.locator(".privacy-badge")).to_have_count(0)
        check_media(guest, base_url, video_id, allowed=True)
        capture(owner_page, artifacts / "lifecycle-video-edited-desktop.png")
        mobile(owner_page)
        capture(owner_page, artifacts / "lifecycle-video-edited-mobile.png")
        owner_page.set_viewport_size({"width": 1440, "height": 900})
        owner_page.locator('.primary-nav a[href="/studio"]').click()
        expect(card).to_contain_text(title)
        expect(card).to_have_attribute("data-video-status", "READY")
        capture(owner_page, artifacts / "lifecycle-studio-desktop.png")
        mobile(owner_page)
        capture(owner_page, artifacts / "lifecycle-studio-mobile.png")
        owner_page.set_viewport_size({"width": 1440, "height": 900})
        card.locator(".manage-video-link").click()
        owner_page.wait_for_url(base_url + watch_path)
        report["video_edit"] = (
            "UI title/description persisted after reload; public/private/public; "
            "anonymous HTML/API/poster/MP4 GET/HEAD/Range denied then restored"
        )

        report["phase"] = "undo_like_and_comment"
        like = owner_page.locator("[data-like]")
        expect(like).to_have_attribute("aria-pressed", "true")
        like.click()
        expect(like).to_have_attribute("aria-pressed", "false")
        expect(owner_page.locator("[data-likes-count]")).to_have_text("0")
        comment = owner_page.locator("[data-comment-id]").filter(has_text=TEST_COMMENT)
        expect(comment).to_have_count(1)
        comment.get_by_role("button", name="Удалить", exact=True).click()
        expect(comment).to_have_count(0)
        expect(owner_page.locator("[data-comments-count]")).to_have_text("0")
        owner_page.reload()
        expect(like).to_have_attribute("aria-pressed", "false")
        expect(owner_page.locator("[data-likes-count]")).to_have_text("0")
        expect(owner_page.locator("[data-comments-count]")).to_have_text("0")
        expect(owner_page.locator("[data-comments-empty]")).to_be_visible()
        expect(owner_page.locator("[data-comments-more]")).to_be_hidden()
        expect(comment).to_have_count(0)
        report["social_undo"] = "unlike and own comment deletion persisted after reload"

        report["phase"] = "rename_private_playlist"
        for path in (playlist_path, f"/api/v1{playlist_path}"):
            expect_status(guest, base_url + path, 200)
        owner_page.goto(base_url + playlist_path)
        owner_page.get_by_text("Настройки плейлиста", exact=True).click()
        playlist_title = f"Изменённый плейлист Chrome {uuid4().hex[:10]}"
        playlist_form = owner_page.locator("[data-playlist-edit]")
        playlist_form.get_by_label("Название", exact=True).fill(playlist_title)
        playlist_form.get_by_label(
            "Кто может видеть плейлист", exact=True
        ).select_option("private")
        with owner_page.expect_navigation(wait_until="load"):
            playlist_form.get_by_role("button", name="Сохранить", exact=True).click()
        owner_page.reload()
        expect(owner_page.get_by_role("heading", level=1)).to_have_text(playlist_title)
        expect(playlist_form.locator('[name="visibility"]')).to_have_value("private")
        renamed = read_json(owner_page.context, base_url + playlist_api)
        require(
            renamed["playlist"]["title"] == playlist_title
            and renamed["playlist"]["visibility"] == "private",
            "Playlist edit was not persisted",
        )
        for path in (playlist_path, f"/api/v1{playlist_path}"):
            expect_status(guest, base_url + path, 404)
        capture(owner_page, artifacts / "lifecycle-playlist-private-desktop.png")

        report["phase"] = "remove_playlist_item"
        with owner_page.expect_navigation(wait_until="load"):
            owner_page.locator(f'[data-remove-playlist-video="{video_id}"]').click()
        expect(owner_page.locator(f'a[href="{watch_path}"]')).to_have_count(0)
        after_remove = read_json(owner_page.context, base_url + playlist_api)
        require(
            {item["id"] for item in after_remove["videos"]["items"]}
            == playlist_ids - {video_id},
            "Playlist removal changed the wrong items",
        )
        require(
            after_remove["playlist"]["videos_count"] == len(playlist_ids) - 1,
            "Playlist removal left an incorrect count",
        )

        report["phase"] = "delete_playlist"
        owner_page.get_by_text("Настройки плейлиста", exact=True).click()
        owner_page.once("dialog", lambda dialog: dialog.accept())
        owner_page.locator("[data-playlist-delete]").click()
        owner_page.wait_for_url(base_url + "/playlists")
        expect(owner_page.locator(f'a[href="{playlist_path}"]')).to_have_count(0)
        for context in (owner_page.context, guest):
            expect_status(context, base_url + playlist_api, 404)
            expect_status(context, base_url + playlist_path, 404)
        for item_id in playlist_ids:
            expect_status(
                owner_page.context, f"{base_url}/api/v1/videos/{item_id}", 200
            )
        report["playlist_lifecycle"] = (
            "UI rename/private persisted; guest HTML/API denied; only target item "
            "removed; playlist deleted; source videos remain available to owner"
        )

        # This destructive step must remain last: earlier checks use this video.
        report["phase"] = "delete_video"
        owner_page.goto(base_url + watch_path)
        expect(owner_page.get_by_role("heading", level=1)).to_have_text(title)
        owner_page.wait_for_function(
            "() => document.querySelector('video')?.readyState >= 2"
        )
        owner_page.locator("video").evaluate("video => video.pause()")
        owner_page.once("dialog", lambda dialog: dialog.accept())
        owner_page.locator("[data-video-delete]").click()
        owner_page.wait_for_url(base_url + "/studio")
        expect(card).to_have_count(0)
        owner_page.reload()
        expect(card).to_have_count(0)
        for context in (owner_page.context, guest):
            check_media(context, base_url, video_id, allowed=False)
            search = context.request.get(
                f"{base_url}/api/v1/videos", params={"q": title}
            )
            try:
                require(
                    search.status == 200
                    and search.json()["total"] == 0
                    and search.json()["items"] == [],
                    "Deleted video remains discoverable",
                )
            finally:
                search.dispose()
        owner_page.get_by_label("Поиск видео по названию", exact=True).fill(title)
        with owner_page.expect_navigation(wait_until="load"):
            owner_page.get_by_role("button", name="Найти видео", exact=True).click()
        expect(owner_page.locator(f'a[href="{watch_path}"]')).to_have_count(0)
        report["video_deletion"] = (
            "UI deletion persisted; absent from studio/search; owner and guest "
            "HTML/API/poster/MP4 GET/HEAD/Range denied"
        )
        require(not errors, "Browser reported JavaScript errors")
        report["viewports"] = ["1440x900", "390x844"]
        report["phase"] = "complete"
        report["status"] = "passed"
        return report
    except Exception as error:
        report["failure_type"] = type(error).__name__
        try:
            owner_page.screenshot(
                path=artifacts / "lifecycle-failure.png",
                full_page=True,
                mask=[owner_page.locator('input[type="password"], .curl-command')],
            )
        except Exception:
            report["failure_screenshot"] = "unavailable"
        raise AssertionError(
            f"Lifecycle failed during {report['phase']} ({type(error).__name__})"
        ) from None
    finally:
        (artifacts / "lifecycle-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        if guest is not None:
            guest.close()
