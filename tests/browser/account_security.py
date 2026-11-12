"""Account controls through real Chrome, API, PostgreSQL and Nginx."""

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from player import no_overflow, require
from playwright.sync_api import Browser, BrowserContext, Page, expect


def screenshot_viewport(page: Page, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => { document.activeElement?.blur(); window.scrollTo(0, 0); }")
    page.wait_for_function(
        """() => {
          if (window.scrollY !== 0) return false;
          if (window.innerWidth >= 900) return true;
          const sidebar = document.querySelector('#site-sidebar');
          return sidebar && document.body.dataset.sidebarOpen === 'false' &&
            sidebar.inert && sidebar.getBoundingClientRect().right <= 1 &&
            getComputedStyle(sidebar).visibility === 'hidden';
        }"""
    )
    no_overflow(page)


def no_session_pagination(page: Page) -> None:
    expect(page.locator("[data-sessions-previous]")).to_be_hidden()
    expect(page.locator("[data-sessions-next]")).to_be_hidden()


def session(context: BrowserContext, base_url: str) -> dict[str, Any]:
    response = context.request.get(base_url + "/api/v1/auth/session")
    require(response.status == 200, "Cannot load disposable account session")
    result: dict[str, Any] = response.json()
    response.dispose()
    return result


def logged_in(context: BrowserContext, base_url: str) -> bool:
    response = context.request.get(base_url + "/api/v1/auth/me")
    status = response.status
    response.dispose()
    require(status in {200, 401}, "Unexpected account authorization response")
    return status == 200


def login(context: BrowserContext, base_url: str, email: str, password: str) -> None:
    state = session(context, base_url)
    response = context.request.post(
        base_url + "/api/v1/auth/login",
        data={"email": email, "password": password},
        headers={"Origin": base_url, "X-CSRF-Token": state["csrf_token"]},
    )
    require(response.status == 200, "Disposable account login failed")
    response.dispose()


def current_session_id(context: BrowserContext, base_url: str) -> str:
    response = context.request.get(base_url + "/api/v1/auth/sessions")
    require(response.status == 200, "Cannot list own active sessions")
    result = response.json()
    response.dispose()
    require(
        set(result) == {"items", "page", "has_more"},
        "Active sessions list changed its public schema",
    )
    for item in result["items"]:
        require(
            set(item) == {"id", "created_at", "expires_at", "current"},
            "Active session list exposes unexpected fields",
        )
    current = [item["id"] for item in result["items"] if item["current"]]
    require(len(current) == 1, "Current session is not uniquely marked")
    return str(current[0])


def session_cookie(context: BrowserContext) -> str:
    cookies = [
        cookie["value"]
        for cookie in context.cookies()
        if cookie["name"] == "foxtube_session"
    ]
    require(len(cookies) == 1, "Account session cookie missing")
    return cookies[0]


def check_account_security(
    browser: Browser, base_url: str, artifacts: Path
) -> dict[str, Any]:
    contexts: list[BrowserContext] = []
    errors: list[str] = []
    report: dict[str, Any] = {"status": "failed"}

    def new_context() -> BrowserContext:
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="ru-RU"
        )
        contexts.append(context)
        context.on(
            "page",
            lambda page: page.on(
                "pageerror", lambda error: errors.append(type(error).__name__)
            ),
        )
        return context

    primary = new_context()
    page = primary.new_page()
    page.set_default_timeout(15000)
    email = f"account-security-{uuid4().hex}@example.com"
    password = secrets.token_urlsafe(32)
    replacement = secrets.token_urlsafe(32)
    try:
        report["phase"] = "registration"
        page.goto(base_url + "/register")
        page.get_by_label("Имя", exact=True).fill("Безопасность аккаунта")
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Создать аккаунт", exact=True).click()
        page.wait_for_url(base_url + "/account")
        expect(page.locator("[data-session-current]")).to_have_count(1)
        no_session_pagination(page)

        report["phase"] = "individual_session_revocation"
        secondary = new_context()
        third = new_context()
        login(secondary, base_url, email, password)
        login(third, base_url, email, password)
        third_id = current_session_id(third, base_url)
        primary_id = current_session_id(primary, base_url)
        before = session_cookie(primary)
        page.reload()
        row = page.locator(f'[data-session-id="{third_id}"]')
        expect(row).to_be_visible()
        with page.expect_response(
            lambda response: (
                response.request.method == "DELETE"
                and urlsplit(response.url).path == f"/api/v1/auth/sessions/{third_id}"
            )
        ) as revoked:
            row.locator("[data-session-revoke]").click()
        require(revoked.value.status == 204, "Individual session revocation failed")
        expect(row).to_have_count(0)
        no_session_pagination(page)
        require(not logged_in(third, base_url), "Revoked session remains authorized")
        require(
            logged_in(secondary, base_url), "Another session was revoked by mistake"
        )
        require(logged_in(primary, base_url), "Current session was revoked by mistake")
        report["individual_session"] = (
            "selected session revoked; other sessions retained"
        )

        report["phase"] = "other_sessions_revocation"
        with page.expect_response(
            base_url + "/api/v1/auth/sessions/revoke-others"
        ) as revoked_others:
            page.locator(
                'form[data-account-action="revoke-others"] button[type="submit"]'
            ).click()
        require(revoked_others.value.status == 200, "Other session revocation failed")
        require(not logged_in(secondary, base_url), "Other session remains authorized")
        require(logged_in(primary, base_url), "Revoke-others removed current session")
        require(
            current_session_id(primary, base_url) == primary_id
            and session_cookie(primary) == before,
            "Revoke-others changed the current session",
        )
        expect(page.locator("[data-session-id]")).to_have_count(1)
        expect(page.locator("[data-session-current]")).to_have_count(1)
        no_session_pagination(page)
        report["other_sessions"] = "other sessions revoked; current session retained"

        report["phase"] = "password_change_validation"
        remaining = new_context()
        login(remaining, base_url, email, password)
        form = page.locator('form[data-account-action="change"]')
        page.locator("#current-password").fill(secrets.token_urlsafe(32))
        page.locator("#new-password").fill(replacement)
        page.locator("#confirm-password").fill(replacement)
        with page.expect_response(
            base_url + "/api/v1/auth/password/change"
        ) as wrong_password:
            form.locator('button[type="submit"]').click()
        require(
            wrong_password.value.status == 403,
            "Wrong current password was accepted",
        )
        expect(form.locator("[data-account-message]")).to_have_text(
            "Текущий пароль указан неверно."
        )
        require(
            logged_in(primary, base_url), "Invalid password changed current session"
        )
        require(
            logged_in(remaining, base_url), "Invalid password revoked other session"
        )
        page.locator("#current-password").fill(password)
        page.locator("#confirm-password").fill(replacement + "x")
        submitted: list[bool] = []

        def record_change(request: Any) -> None:
            if urlsplit(request.url).path == "/api/v1/auth/password/change":
                submitted.append(True)

        page.on("request", record_change)
        form.locator('button[type="submit"]').click()
        expect(form.locator("[data-account-message]")).to_have_text(
            "Пароли не совпадают."
        )
        require(not submitted, "Mismatched confirmation was submitted")
        page.remove_listener("request", record_change)

        report["phase"] = "password_change_rotation"
        page.locator("#confirm-password").fill(replacement)
        with page.expect_response(base_url + "/api/v1/auth/password/change") as changed:
            form.locator('button[type="submit"]').click()
        require(changed.value.status == 200, "Password change failed")
        expect(form.locator("[data-account-message]")).to_have_text(
            "Пароль изменён. Другие сеансы завершены."
        )
        for selector in ("#current-password", "#new-password", "#confirm-password"):
            expect(page.locator(selector)).to_have_value("")
        require(
            session_cookie(primary) != before, "Password change did not rotate cookie"
        )
        require(
            current_session_id(primary, base_url) != primary_id,
            "Password change did not rotate session identity",
        )
        require(logged_in(primary, base_url), "Password change lost current login")
        require(not logged_in(remaining, base_url), "Password change kept other login")
        old_password = new_context()
        state = session(old_password, base_url)
        rejected = old_password.request.post(
            base_url + "/api/v1/auth/login",
            data={"email": email, "password": password},
            headers={"Origin": base_url, "X-CSRF-Token": state["csrf_token"]},
        )
        require(rejected.status == 401, "Previous password still permits login")
        rejected.dispose()
        fresh = new_context()
        fresh_page = fresh.new_page()
        fresh_page.goto(base_url + "/login")
        fresh_page.get_by_label("Email", exact=True).fill(email)
        fresh_page.get_by_label("Пароль", exact=True).fill(replacement)
        fresh_page.get_by_role("button", name="Войти", exact=True).click()
        fresh_page.wait_for_url(base_url + "/account")
        require(
            logged_in(fresh, base_url), "Replacement password does not permit login"
        )
        report["password_change"] = (
            "wrong password and mismatch rejected; current session rotated; "
            "others revoked; previous password denied; new password accepted"
        )

        report["phase"] = "csrf"
        rejected = primary.request.post(
            base_url + "/api/v1/auth/sessions/revoke-others", data={}
        )
        require(rejected.status == 403, "Session management accepts missing CSRF")
        rejected.dispose()
        state = session(primary, base_url)
        rejected = primary.request.post(
            base_url + "/api/v1/auth/sessions/revoke-others",
            data={},
            headers={
                "Origin": "https://untrusted.example",
                "X-CSRF-Token": state["csrf_token"],
            },
        )
        require(rejected.status == 403, "Session management accepts foreign Origin")
        rejected.dispose()
        require(logged_in(fresh, base_url), "Rejected CSRF request revoked a session")
        report["csrf"] = "missing token and foreign Origin rejected without mutation"

        report["phase"] = "account_mobile"
        page.reload()
        expect(page.locator("[data-session-current]")).to_have_count(1)
        for width, height in [(1440, 900), (390, 844), (320, 740)]:
            screenshot_viewport(page, width, height)
            page.screenshot(
                path=artifacts / f"account-security-{width}.png",
                full_page=True,
                animations="disabled",
                mask=[page.locator("input")],
            )

        report["phase"] = "password_recovery"
        recovery_context = new_context()
        recovery = recovery_context.new_page()
        recovery.set_default_timeout(15000)
        recovery.goto(base_url + "/login")
        recovery.locator('a[href="/password/forgot"]').click()
        recovery.wait_for_url(base_url + "/password/forgot")
        recovery.locator("#recovery-email").fill(email)
        with recovery.expect_response(
            base_url + "/api/v1/auth/password/forgot"
        ) as requested:
            recovery.locator(
                'form[data-account-action="forgot"] button[type="submit"]'
            ).click()
        require(requested.value.status == 503, "Unconfigured SMTP was not reported")
        expect(recovery.locator("[data-account-message]")).to_have_text(
            "Восстановление пароля пока недоступно. Попробуйте позже."
        )
        recovery.locator("#recovery-email").fill("")
        for width, height in [(1440, 900), (390, 844), (320, 740)]:
            screenshot_viewport(recovery, width, height)
            recovery.screenshot(
                path=artifacts / f"password-forgot-{width}.png",
                full_page=True,
                animations="disabled",
            )

        report["phase"] = "password_reset_fragment"
        token = secrets.token_urlsafe(32)
        token_in_urls: list[bool] = []
        recovery.on(
            "request", lambda request: token_in_urls.append(token in request.url)
        )
        recovery.goto(base_url + "/password/reset#token=" + token)
        recovery.wait_for_function("() => location.hash === ''")
        require(not any(token_in_urls), "Recovery token leaked into a request URL")
        recovery.locator("#reset-new-password").fill(secrets.token_urlsafe(32))
        recovery.locator("#reset-confirm-password").fill(
            recovery.locator("#reset-new-password").input_value()
        )
        with recovery.expect_response(
            base_url + "/api/v1/auth/password/reset"
        ) as invalid_reset:
            recovery.locator(
                'form[data-account-action="reset"] button[type="submit"]'
            ).click()
        require(
            invalid_reset.value.status in {400, 422},
            "Random recovery token was accepted",
        )
        expect(recovery.locator("[data-account-message]")).to_be_visible()
        require(not logged_in(recovery_context, base_url), "Invalid reset logged in")
        for selector in ("#reset-new-password", "#reset-confirm-password"):
            recovery.locator(selector).fill("")
        require(not any(token_in_urls), "Recovery token leaked into a request URL")
        for width, height in [(1440, 900), (390, 844), (320, 740)]:
            screenshot_viewport(recovery, width, height)
            recovery.screenshot(
                path=artifacts / f"password-reset-{width}.png",
                full_page=True,
                animations="disabled",
            )
        recovery.reload()
        expect(
            recovery.locator('form[data-account-action="reset"] button[type="submit"]')
        ).to_be_disabled()
        expect(recovery.locator('a[href="/password/forgot"]').first).to_be_visible()
        report["recovery"] = (
            "unconfigured SMTP reported; fragment removed before subsequent requests; "
            "invalid token rejected; reload requires a new recovery link"
        )
        report["viewports"] = ["1440x900", "390x844", "320x740"]
        require(not errors, "Account controls produced JavaScript exceptions")
        report["javascript_errors"] = errors
        report["status"] = "passed"
        return report
    finally:
        if report["status"] != "passed":
            for index, context in enumerate(contexts):
                for page_index, open_page in enumerate(context.pages):
                    try:
                        open_page.screenshot(
                            path=artifacts
                            / f"account-failure-{index}-{page_index}.png",
                            full_page=True,
                            mask=[open_page.locator("input")],
                        )
                    except Exception:
                        pass
        (artifacts / "account-security.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        for context in contexts:
            context.close()
