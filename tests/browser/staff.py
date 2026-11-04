"""Staff fixture and real browser checks for the disposable Compose project."""

import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Locator, Page

ADMIN_EMAIL = "browser-admin@example.com"
ADMIN_PASSWORD = "Disposable-staff-browser-passphrase-73!"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def prepare_staff() -> None:
    # This file also runs through stdin in the runtime API image, which has no
    # Playwright installed. All fixture writes still pass through the real API.
    from app.core.config import get_settings
    from app.demo import Browser

    settings = get_settings()
    origin = str(settings.public_origin).rstrip("/")
    require(
        settings.env == "test" and urlsplit(origin).hostname == "nginx",
        "Staff fixtures must only target the disposable Compose test project",
    )
    client = Browser(origin)
    client.sign_in(
        {
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "name": "Администратор Chrome",
        }
    )
    client.call("POST", "/api/v1/auth/logout", {})
    print("Disposable staff account registered through the real API.")


def assert_no_overflow(page: "Page") -> None:
    require(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        ),
        "Staff page has horizontal overflow at the tested viewport",
    )


def use_mobile_viewport(page: "Page") -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        """() => {
          const sidebar = document.querySelector('#site-sidebar');
          return document.body.dataset.sidebarOpen === 'false'
            && sidebar.getBoundingClientRect().right <= 1;
        }"""
    )


def check_moderation(
    browser: "Browser",
    base_url: str,
    video_id: str,
    viewer_page: "Page",
    viewer_email: str,
    errors: list[str],
) -> dict[str, object]:
    from playwright.sync_api import expect

    artifacts = Path(os.environ.get("FOXTUBE_BROWSER_ARTIFACTS", "/artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    admin_context = browser.new_context(
        locale="ru-RU", viewport={"width": 1440, "height": 900}
    )
    guest_context = browser.new_context(locale="ru-RU")
    admin = admin_context.new_page()
    admin.set_default_timeout(15000)
    admin.on("pageerror", lambda error: errors.append(type(error).__name__))

    def lookup(email: str) -> "Locator":
        form = admin.locator("[data-role-lookup]")
        form.locator('[name="kind"]').select_option("email")
        form.locator('[name="target"]').fill(email)
        form.get_by_role("button", name="Найти аккаунт", exact=True).click()
        role_form = admin.locator("[data-role-change]")
        expect(role_form).to_be_visible()
        expect(role_form.locator("[data-role-target]")).to_contain_text(email)
        return role_form

    def check_guest_media(*, hidden: bool) -> None:
        expected = 404 if hidden else 200
        metadata = guest_context.request.get(f"{base_url}/api/v1/videos/{video_id}")
        require(metadata.status == expected, "Wrong public metadata moderation ACL")
        metadata.dispose()
        for kind in ("playback", "poster"):
            url = f"{base_url}/media/videos/{video_id}/{kind}"
            head = guest_context.request.head(url)
            require(head.status == expected, "Wrong public media HEAD moderation ACL")
            head.dispose()
            ranged = guest_context.request.get(url, headers={"Range": "bytes=0-63"})
            require(
                ranged.status == (404 if hidden else 206),
                "Wrong public media Range moderation ACL",
            )
            if not hidden:
                require(
                    ranged.headers.get("content-range", "").startswith("bytes 0-63/"),
                    "Restored media did not serve the requested byte range",
                )
            ranged.dispose()

    try:
        admin.goto(f"{base_url}/login")
        admin.get_by_label("Email", exact=True).fill(ADMIN_EMAIL)
        admin.get_by_label("Пароль", exact=True).fill(ADMIN_PASSWORD)
        admin.get_by_role("button", name="Войти", exact=True).click()
        admin.wait_for_url(f"{base_url}/account")
        admin.goto(f"{base_url}/admin")
        role_form = lookup(viewer_email)
        expect(role_form.locator('[name="role"]')).to_have_value("user")
        role_form.locator('[name="role"]').select_option("moderator")
        role_form.locator('[name="reason"]').fill(
            "Назначение модератора через настоящий интерфейс Chrome."
        )
        with admin.expect_navigation(wait_until="load"):
            role_form.get_by_role("button", name="Изменить роль", exact=True).click()
        for title in (
            "Роль изменена",
            "Создана жалоба",
            "Первый администратор назначен",
        ):
            expect(admin.locator(".audit-event").filter(has_text=title)).to_be_visible()
        role_form = lookup(viewer_email)
        expect(role_form.locator('[name="role"]')).to_have_value("moderator")

        # The already logged-in viewer must gain the current role without a
        # replacement session or a second login.
        viewer_page.reload()
        expect(viewer_page.locator('a[href="/moderation"]').first).to_be_visible()
        viewer_page.goto(f"{base_url}/moderation")
        card = viewer_page.locator(f'[data-report-card][data-video-id="{video_id}"]')
        expect(card).to_be_visible()
        moderation = card.locator("[data-video-moderation]")
        moderation.locator('[name="status"]').select_option("hidden")
        moderation.locator('[name="reason"]').fill(
            "Проверка запрета новых запросов после скрытия."
        )
        with viewer_page.expect_navigation(wait_until="load"):
            moderation.get_by_role(
                "button", name="Применить к видео", exact=True
            ).click()
        expect(card.locator(".moderation-state dd").nth(1)).to_have_text("Скрыто")
        check_guest_media(hidden=True)

        moderation.locator('[name="status"]').select_option("visible")
        moderation.locator('[name="reason"]').fill(
            "Проверка восстановления исходного доступа."
        )
        with viewer_page.expect_navigation(wait_until="load"):
            moderation.get_by_role(
                "button", name="Применить к видео", exact=True
            ).click()
        expect(card.locator(".moderation-state dd").nth(1)).to_have_text("Не скрыто")
        check_guest_media(hidden=False)
        assert_no_overflow(viewer_page)
        viewer_page.screenshot(
            path=artifacts / "moderation-desktop.png", full_page=True
        )
        use_mobile_viewport(viewer_page)
        assert_no_overflow(viewer_page)
        viewer_page.screenshot(path=artifacts / "moderation-mobile.png", full_page=True)

        resolution = card.locator("[data-report-resolution]")
        resolution.locator('[name="resolution"]').fill(
            "Проверено: скрытие блокирует MP4 и превью; восстановление работает."
        )
        with viewer_page.expect_navigation(wait_until="load"):
            resolution.get_by_role("button", name="Закрыть жалобу", exact=True).click()
        expect(card).to_have_count(0)
        viewer_page.goto(f"{base_url}/moderation?closed=true")
        expect(card).to_be_visible()
        expect(card.locator("[data-report-resolution]")).to_have_count(0)
        expect(card).to_contain_text("Проверено: скрытие блокирует MP4 и превью")

        admin.goto(f"{base_url}/admin")
        for title in (
            "Закрыта жалоба",
            "Видео восстановлено",
            "Видео скрыто",
            "Роль изменена",
        ):
            expect(admin.locator(".audit-event").filter(has_text=title)).to_be_visible()
        assert_no_overflow(admin)
        admin.screenshot(path=artifacts / "admin-desktop.png", full_page=True)

        role_form = lookup(ADMIN_EMAIL)
        expect(role_form.locator('[name="role"]')).to_have_value("admin")
        role_form.locator('[name="role"]').select_option("user")
        role_form.locator('[name="reason"]').fill(
            "Проверка запрета понижения последнего администратора."
        )
        with admin.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and urlsplit(response.url).path == "/api/v1/admin/roles"
            )
        ) as rejected:
            role_form.get_by_role("button", name="Изменить роль", exact=True).click()
        require(rejected.value.status == 409, "Last admin demotion was not rejected")
        expect(role_form.locator("[data-moderation-message]")).to_be_visible()
        expect(role_form.locator("[data-moderation-message]")).to_have_class(
            "moderation-message is-error"
        )
        admin.reload()
        role_form = lookup(ADMIN_EMAIL)
        expect(role_form.locator('[name="role"]')).to_have_value("admin")
        use_mobile_viewport(admin)
        assert_no_overflow(admin)
        admin.screenshot(path=artifacts / "admin-mobile.png", full_page=True)
        return {
            "role_assignment": "persisted through UI; active session rechecked",
            "moderation": "hide/restore through UI; guest HEAD/Range 404 then 200/206",
            "resolution": "persisted through UI; closed report visible",
            "audit": "role change, hide, restore and resolution visible",
            "last_admin": "UI received 409; admin role preserved",
            "viewports": ["1440x900", "390x844"],
        }
    finally:
        guest_context.close()
        admin_context.close()


if __name__ == "__main__":
    prepare_staff()
