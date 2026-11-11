"""UI preferences/auth checks on disposable data with actual Chrome navigation."""

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from miniplayer import close_mini, remember_video, same_video
from player import no_overflow, ready, require
from player_extended import wait_elapsed
from playwright.sync_api import Browser, Error, Page, Route, expect
from staff import ADMIN_EMAIL, ADMIN_PASSWORD

SIDEBAR_KEY = "foxtube.sidebar.expanded.v1"


def settled_geometry(page: Page) -> None:
    """Capture the final layout, allowing real CSS transitions/fonts to finish."""
    page.evaluate("async () => {await document.fonts.ready;}")
    page.evaluate("() => {delete window.__experienceGeometry;}")
    page.wait_for_function(
        """() => {
          const elements = document.querySelectorAll(
            '#main-content,#site-sidebar,.watch-page,.auth-layout,.auth-card');
          const geometry = JSON.stringify([innerWidth, innerHeight,
            ...[...elements].flatMap(element => {
              const r = element.getBoundingClientRect();
              return [r.x, r.y, r.width, r.height].map(n => Math.round(n * 100));
            })]);
          const now = performance.now();
          const previous = window.__experienceGeometry;
          if (!previous || previous.geometry !== geometry) {
            window.__experienceGeometry = {geometry, since: now};
            return false;
          }
          const transitioning = document.getAnimations().some(animation =>
            animation instanceof CSSTransition && animation.playState === 'running');
          return !transitioning && now - previous.since >= 200;
        }""",
        polling="raf",
        timeout=5000,
    )
    page.evaluate("() => {delete window.__experienceGeometry;}")


def sidebar(page: Page, expanded: bool) -> None:
    expect(page.locator("body")).to_have_attribute(
        "data-sidebar-open", str(expanded).lower()
    )
    expect(page.locator("[data-menu-toggle]")).to_have_attribute(
        "aria-expanded", str(expanded).lower()
    )
    settled_geometry(page)
    no_overflow(page)


def password_toggle(page: Page, password: str, autocomplete: str) -> None:
    field = page.get_by_label("Пароль", exact=True)
    button = page.locator("[data-password-toggle]")
    expect(button).to_have_attribute("type", "button")
    expect(button).to_have_attribute("aria-controls", field.get_attribute("id") or "")
    expect(field).to_have_attribute("autocomplete", autocomplete)
    expect(field).to_have_attribute("type", "password")
    expect(button).to_have_attribute("aria-pressed", "false")
    expect(button).to_have_attribute("aria-label", "Показать пароль")
    field.fill(password)
    button.click()
    expect(field).to_have_attribute("type", "text")
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(button).to_have_attribute("aria-label", "Скрыть пароль")
    require(field.input_value() == password, "Reveal changed the password value")
    button.press("Enter")
    expect(field).to_have_attribute("type", "password")
    expect(button).to_have_attribute("aria-pressed", "false")
    expect(button).to_have_attribute("aria-label", "Показать пароль")
    require(field.input_value() == password, "Hide changed the password value")
    require(
        page.locator('[data-auth-form="logout"]').count() == 0,
        "Password visibility accidentally submitted an auth form",
    )


def auth_experience(
    browser: Browser, base_url: str, artifacts: Path, errors: list[str]
) -> dict[str, str]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, locale="ru-RU"
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    try:
        page.goto(base_url + "/register")
        # Screenshots contain empty forms; credentials never enter artifacts.
        for width, height in [(1440, 900), (390, 844), (320, 740)]:
            page.set_viewport_size({"width": width, "height": height})
            settled_geometry(page)
            no_overflow(page)
            expect(page.locator("[data-password-toggle]")).to_be_visible()
            page.screenshot(
                path=artifacts / f"experience-register-{width}.png", full_page=True
            )
        email = f"experience-{uuid4().hex}@example.com"
        password = secrets.token_urlsafe(32)
        page.get_by_label("Имя", exact=True).fill("Проверка интерфейса")
        page.get_by_label("Email", exact=True).fill(email)
        password_toggle(page, password, "new-password")
        page.get_by_role("button", name="Создать аккаунт", exact=True).click()
        page.wait_for_url(base_url + "/account")
        page.locator('[data-auth-form="logout"] button').click()
        page.wait_for_url(base_url + "/")
        page.locator('.login-link[href="/login"]').click()
        page.wait_for_url(base_url + "/login")
        settled_geometry(page)
        no_overflow(page)
        page.screenshot(path=artifacts / "experience-login-320.png", full_page=True)
        page.get_by_label("Email", exact=True).fill(email)
        password_toggle(page, password, "current-password")
        page.get_by_role("button", name="Войти", exact=True).click()
        page.wait_for_url(base_url + "/account")
        expect(page.locator('[data-auth-form="logout"]')).to_be_visible()
        return {
            "password": "Mouse/keyboard reveal/hide; aria, autocomplete/value retained",
            "auth": "Actual register/logout/login forms submitted on a 320px viewport",
        }
    except BaseException:
        page.screenshot(
            path=artifacts / "experience-auth-failure.png",
            full_page=True,
            mask=[page.locator('[name="password"], [name="email"]')],
        )
        raise
    finally:
        context.close()


def menu_experience(page: Page, base_url: str, watch_path: str, artifacts: Path) -> str:
    page.goto(base_url + "/")
    if page.locator("body").get_attribute("data-sidebar-open") != "true":
        page.locator("[data-menu-toggle]").click()
    sidebar(page, True)
    page.locator("[data-menu-toggle]").click()
    sidebar(page, False)
    page.screenshot(path=artifacts / "experience-sidebar-desktop.png", full_page=True)
    page.locator('.primary-nav a[href="/search"]').click()
    page.wait_for_url(base_url + "/search")
    sidebar(page, False)
    page.reload()
    sidebar(page, False)
    require(
        page.evaluate("key => localStorage.getItem(key)", SIDEBAR_KEY) == "false",
        "Desktop collapse was not persisted",
    )
    page.goto(base_url + watch_path)
    ready(page)
    sidebar(page, False)
    remember_video(page)
    page.locator("[data-fox-mini]").click()
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    same_video(page)
    sidebar(page, False)
    page.set_viewport_size({"width": 390, "height": 844})
    sidebar(page, False)
    page.locator("[data-menu-toggle]").click()
    sidebar(page, True)
    expect(page.locator("[data-menu-backdrop]")).to_be_visible()
    page.screenshot(path=artifacts / "experience-sidebar-mobile.png", full_page=True)
    page.keyboard.press("Escape")
    sidebar(page, False)
    expect(page.locator("[data-menu-toggle]")).to_be_focused()
    page.locator("[data-menu-toggle]").click()
    page.locator('.primary-nav a[href="/search"]').click()
    page.wait_for_url(base_url + "/search")
    sidebar(page, False)
    same_video(page)
    require(
        page.evaluate("key => localStorage.getItem(key)", SIDEBAR_KEY) == "false",
        "Mobile drawer overwrote the desktop preference",
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    sidebar(page, False)
    close_mini(page)
    page.reload()
    sidebar(page, False)
    page.locator("[data-menu-toggle]").click()
    sidebar(page, True)
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    page.reload()
    sidebar(page, True)
    return "Native/mini nav/reload persisted; mobile drawer kept desktop preference"


def menu_without_storage(
    browser: Browser, owner: Page, base_url: str, watch_path: str, errors: list[str]
) -> str:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
        storage_state=owner.context.storage_state(),
    )
    context.add_init_script(
        """Object.defineProperty(window, 'localStorage', {
          get() {throw new DOMException('Test storage unavailable', 'SecurityError');}
        });"""
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    try:
        page.goto(base_url + watch_path)
        ready(page)
        sidebar(page, True)
        page.locator("[data-menu-toggle]").click()
        sidebar(page, False)
        remember_video(page)
        page.locator("[data-fox-mini]").click()
        page.locator('.brand[href="/"]').click()
        page.wait_for_url(base_url + "/")
        sidebar(page, False)
        same_video(page)
        page.set_viewport_size({"width": 390, "height": 844})
        sidebar(page, False)
        page.locator("[data-menu-toggle]").click()
        sidebar(page, True)
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1440, "height": 900})
        sidebar(page, False)
        close_mini(page)
        return "Unavailable storage: memory preference survives soft nav/mobile resize"
    finally:
        context.close()


def check_experience(
    browser: Browser,
    owner: Page,
    base_url: str,
    watch_path: str,
    source: Path,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "failed", "phase": "auth"}
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
        storage_state=owner.context.storage_state(),
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    try:
        report["auth"] = auth_experience(browser, base_url, artifacts, errors)
        report["phase"] = "sidebar"
        report["sidebar"] = menu_experience(page, base_url, watch_path, artifacts)
        report["phase"] = "sidebar_without_storage"
        report["sidebar_without_storage"] = menu_without_storage(
            browser, owner, base_url, watch_path, errors
        )
        report["phase"] = "recommendations"
        report["recommendations"] = recommendations_experience(
            browser, page, base_url, watch_path, source, artifacts, errors
        )
        report["status"] = "passed"
        return report
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        page.screenshot(path=artifacts / "experience-failure.png", full_page=True)
        raise
    finally:
        context.close()
        (artifacts / "experience.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )


def fixture_upload(page: Page, base_url: str, source: Path) -> dict[str, Any]:
    page.goto(base_url + "/upload")
    title = f"Похожее видео Chrome {uuid4().hex[:10]}"
    page.locator('input[type="file"]').set_input_files(source)
    page.locator('[name="title"]').fill(title)
    page.locator('[name="description"]').fill("Реальный H.264 для следующего видео")
    page.locator('[name="visibility"]').select_option("public")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/api/v1/videos"
        )
    ) as created:
        page.locator('form:has(input[type="file"]) button[type="submit"]').click()
    require(created.value.status == 201, "Recommendation fixture metadata failed")
    video = created.value.json()
    path = f"/watch/{video['id']}"
    link = page.locator(f'[data-upload-watch][href="{path}"]')
    expect(link).to_be_visible(timeout=120000)
    link.click()
    page.wait_for_url(base_url + path)
    ready(page)
    return video


def mutation(page: Page, base_url: str, method: str, path: str, data: Any) -> Any:
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    require(bool(token), "Recommendation fixture has no CSRF token")
    return page.context.request.fetch(
        base_url + path,
        method=method,
        data=data,
        headers={"Origin": base_url, "X-CSRF-Token": token or ""},
    )


def recommendation_ids(page: Page, base_url: str, video_id: str) -> list[str]:
    response = page.context.request.get(
        f"{base_url}/api/v1/videos/{video_id}/recommendations?limit=12"
    )
    require(response.status == 200, "Recommendations API failed for a readable source")
    items = response.json()["items"]
    response.dispose()
    require(all(item["status"] == "READY" for item in items), "Non-READY recommended")
    require(
        all(item["playback_url"] and item["poster_url"] for item in items),
        "Recommendations contain an unplayable video",
    )
    ids = [item["id"] for item in items]
    require(video_id not in ids and len(ids) == len(set(ids)), "Current/duplicate item")
    return ids


def ended(page: Page) -> None:
    """Seek the actual H.264, then let the decoder emit its own ended event."""
    ready(page)
    video = page.locator("[data-fox-player] video")
    video.evaluate(
        """v => {
          window.__experienceEndedVideo = v;
          window.__experienceEnded = 0;
          v.addEventListener('ended', () => window.__experienceEnded++, {once:true});
          v.currentTime = v.duration - .45;
        }"""
    )
    if video.evaluate("v => v.paused"):
        # Mini deliberately hides the large center overlay; its real toolbar
        # play button remains available in both player modes.
        page.locator("[data-fox-player]").hover()
        toggle = page.locator("[data-fox-toggle]")
        expect(toggle).to_be_visible()
        toggle.click()
    page.wait_for_function(
        "() => window.__experienceEnded === 1 && "
        "window.__experienceEndedVideo.ended && !window.__experienceEndedVideo.error"
    )


def next_playing(page: Page, video_id: str) -> None:
    page.wait_for_function(
        """id => {
          const root = document.querySelector('[data-fox-player]');
          const v = root?.querySelector('video');
          return root?.dataset.playerVideo === id && v?.readyState >= 2 &&
            !v.paused && !v.error && v.currentTime > .3;
        }""",
        arg=video_id,
        timeout=30000,
    )
    settled_geometry(page)
    geometry = page.evaluate(
        """() => {
          const sidebar = document.querySelector('#site-sidebar');
          const main = document.querySelector('#main-content');
          const article = main?.querySelector('.watch-page');
          return {
            desktop: matchMedia('(min-width: 900px)').matches,
            expanded: document.body.dataset.sidebarOpen === 'true',
            sidebarRight: sidebar?.getBoundingClientRect().right ?? null,
            mainLeft: main?.getBoundingClientRect().left ?? null,
            articleLeft: article?.getBoundingClientRect().left ?? null
          };
        }"""
    )
    if geometry["desktop"]:
        require(
            geometry["sidebarRight"] is not None
            and geometry["mainLeft"] is not None
            and geometry["mainLeft"] >= geometry["sidebarRight"] - 0.5,
            f"Autoplay main overlaps desktop sidebar: {geometry}",
        )
        if geometry["expanded"] and geometry["articleLeft"] is not None:
            require(
                geometry["articleLeft"] >= geometry["sidebarRight"] - 0.5,
                f"Autoplay watch article overlaps expanded sidebar: {geometry}",
            )
    require(
        page.evaluate(
            "() => [...document.querySelectorAll('video,audio')]"
            ".filter(v => !v.paused && !v.muted && v.volume>0).length<=1"
        ),
        "Autoplay produced concurrent audio",
    )
    no_overflow(page)


def autoplay_experience(
    page: Page, base_url: str, source_id: str, target: dict[str, Any], artifacts: Path
) -> dict[str, str]:
    watch_path = f"/watch/{source_id}"
    target_id = target["id"]
    target_path = f"/watch/{target_id}"
    page.bring_to_front()
    page.goto(base_url + watch_path)
    page.wait_for_function("() => !document.hidden")
    expect(page.locator("[data-autoplay-toggle]:visible").first).to_be_checked()
    links = page.locator("[data-recommendation-video]")
    require(
        target_id
        in links.evaluate_all("links=>links.map(a=>a.dataset.recommendationVideo)"),
        "Accessible related video missing from actual watch HTML",
    )
    expect(page.locator("[data-recommendation-next]")).to_have_attribute(
        "href", target_path
    )
    remember_video(page)
    ended(page)
    expect(page.locator("[data-up-next]")).to_be_visible()
    expect(page.locator("[data-up-next-title]")).to_have_text(target["title"])
    first_count = int(page.locator("[data-up-next-countdown]").inner_text())
    require(1 <= first_count <= 5, "Countdown did not start between 5 and 1")
    page.wait_for_function(
        "first => Number(document.querySelector('[data-up-next-countdown]')"
        ".textContent) < first",
        arg=first_count,
    )
    page.locator("[data-up-next-cancel]").click()
    expect(page.locator("[data-up-next]")).to_be_hidden()
    wait_elapsed(page, 5600)
    require(page.url == base_url + watch_path, "Cancelled countdown navigated")
    same_video(page)

    # Off is a persisted UI preference, not a test-only autoplay browser flag.
    page.locator("[data-autoplay-toggle]:visible").first.uncheck()
    page.reload()
    expect(page.locator("[data-autoplay-toggle]:visible").first).not_to_be_checked()
    ended(page)
    expect(page.locator("[data-up-next]")).to_be_hidden()
    wait_elapsed(page, 5600)
    require(page.url == base_url + watch_path, "Autoplay off started the next video")
    require(
        page.evaluate("() => localStorage.getItem('foxtube.autoplay.v1')") == "false",
        "Autoplay off preference was not persisted",
    )
    page.locator("[data-autoplay-toggle]:visible").first.check()

    # A real pending target HTTP request may complete after the user cancels.
    page.goto(base_url + watch_path)
    held: list[Route] = []
    target_url = f"{base_url}/api/v1/videos/{target_id}"

    def hold(route: Route) -> None:
        held.append(route)
        page.evaluate("() => {window.__experienceTargetHeld=true;}")

    page.route(target_url, hold)
    try:
        remember_video(page)
        ended(page)
        page.wait_for_function("() => window.__experienceTargetHeld === true")
        require(len(held) == 1, "Next target metadata was not requested exactly once")
        page.locator("[data-up-next-cancel]").click()
    finally:
        for route in held:
            try:
                route.continue_()
            except Error:
                require(
                    route.request.failure == "net::ERR_ABORTED",
                    "Held target failed for a reason other than cancellation",
                )
        page.unroute(target_url, hold)
    wait_elapsed(page, 900)
    require(page.url == base_url + watch_path, "Late target response defeated cancel")
    same_video(page)
    expect(page.locator("[data-up-next]")).to_be_hidden()

    page.goto(base_url + watch_path)
    ended(page)
    expect(page.locator("[data-up-next]")).to_be_visible()
    page.wait_for_url(base_url + target_path, timeout=20000)
    next_playing(page, target_id)
    page.screenshot(path=artifacts / "experience-autoplay-desktop.png", full_page=True)

    # In mini mode the same page must stay open while the next actual video starts.
    page.goto(base_url + watch_path)
    ready(page)
    page.locator("[data-fox-mini]").click()
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    page.evaluate("() => {window.__experienceHomeDocument=document;}")
    ended(page)
    expect(page.locator("[data-up-next]")).to_be_visible()
    page.locator("[data-up-next-now]").click()
    next_playing(page, target_id)
    require(page.url == base_url + "/", "Mini next replaced the open page URL")
    require(
        page.evaluate("() => window.__experienceHomeDocument===document"),
        "Mini next reloaded the open page document",
    )
    expect(page.locator("[data-mini-title]")).to_have_text(target["title"])
    expect(page.locator("#page-title")).to_have_text("Главная")
    remember_video(page)
    close_mini(page)

    for width, height in [(390, 844), (320, 740)]:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(base_url + watch_path)
        ended(page)
        expect(page.locator("[data-up-next]")).to_be_visible()
        no_overflow(page)
        expect(page.locator("[data-up-next-cancel]")).to_be_visible()
        page.screenshot(
            path=artifacts / f"experience-up-next-{width}.png", full_page=True
        )
        page.locator("[data-up-next-now]").click()
        page.wait_for_url(base_url + target_path)
        next_playing(page, target_id)
    return {
        "cancel": "Real ended countdown cancelled; no navigation after 5 seconds",
        "disabled": "Toggle off persisted across reload; real ended did not start next",
        "cancel_pending_http": "Held original target GET: cancel defeats late response",
        "main": "Real ended/countdown, next time > .3; settled sidebar/main layout",
        "mini": "Play now advances actual next; home URL/document retained",
        "mobile": "390/320 overlay fits; play now advances actual next",
    }


def recommendations_experience(
    browser: Browser,
    owner: Page,
    base_url: str,
    watch_path: str,
    source: Path,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    require(
        urlsplit(base_url).hostname == "nginx", "Fixtures require disposable Compose"
    )
    guest = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU")
    admin_context = browser.new_context(locale="ru-RU")
    page = guest.new_page()
    admin = admin_context.new_page()
    for surface in (page, admin):
        surface.set_default_timeout(15000)
        surface.on("pageerror", lambda error: errors.append(type(error).__name__))
    target: dict[str, Any] | None = None
    reserve_id: str | None = None
    source_id = watch_path.rsplit("/", 1)[-1]
    try:
        target = fixture_upload(owner, base_url, source)
        target_id = target["id"]
        candidate_path = f"/api/v1/videos/{target_id}"
        require(
            target_id in recommendation_ids(page, base_url, source_id),
            "Public READY candidate missing from guest recommendations",
        )
        changed = mutation(
            owner, base_url, "PATCH", candidate_path, {"visibility": "private"}
        )
        require(changed.status == 200, "Private recommendation fixture mutation failed")
        changed.dispose()
        require(
            target_id not in recommendation_ids(page, base_url, source_id),
            "Private candidate leaked to a guest",
        )
        page.goto(base_url + watch_path)
        expect(
            page.locator(f'[data-recommendation-video="{target_id}"]')
        ).to_have_count(0)
        denied = guest.request.get(base_url + candidate_path + "/recommendations")
        require(denied.status == 404, "Private source recommendations bypass ACL")
        denied.dispose()
        changed = mutation(
            owner, base_url, "PATCH", candidate_path, {"visibility": "public"}
        )
        require(changed.status == 200, "Public recommendation fixture restore failed")
        changed.dispose()

        reserved = mutation(
            owner,
            base_url,
            "POST",
            "/api/v1/videos",
            {
                "title": f"Незагруженное похожее {uuid4().hex[:10]}",
                "description": "Только резерв; без файла и задания",
                "visibility": "public",
                "expected_size": source.stat().st_size,
            },
        )
        require(reserved.status == 201, "Non-READY recommendation fixture failed")
        reserve = reserved.json()
        reserved.dispose()
        reserve_id = reserve["id"]
        require(reserve["status"] != "READY", "Non-READY reserve unexpectedly ready")
        require(
            reserve_id not in recommendation_ids(page, base_url, source_id),
            "Non-READY public candidate leaked to guest recommendations",
        )
        page.reload()
        expect(
            page.locator(f'[data-recommendation-video="{reserve_id}"]')
        ).to_have_count(0)
        require(
            recommendation_ids(owner, base_url, reserve_id) == [],
            "Owner's non-READY source has recommendations",
        )

        admin.goto(base_url + "/login")
        admin.get_by_label("Email", exact=True).fill(ADMIN_EMAIL)
        admin.get_by_label("Пароль", exact=True).fill(ADMIN_PASSWORD)
        admin.get_by_role("button", name="Войти", exact=True).click()
        admin.wait_for_url(base_url + "/account")
        moderation_path = f"/api/v1/moderation/videos/{target_id}"
        hidden = mutation(
            admin,
            base_url,
            "PATCH",
            moderation_path,
            {"status": "hidden", "reason": "QA: исключение из рекомендаций"},
        )
        require(hidden.status == 200, "Hidden recommendation fixture mutation failed")
        hidden.dispose()
        require(
            target_id not in recommendation_ids(page, base_url, source_id),
            "Hidden candidate leaked to guest recommendations",
        )
        page.reload()
        expect(
            page.locator(f'[data-recommendation-video="{target_id}"]')
        ).to_have_count(0)
        denied = guest.request.get(base_url + candidate_path + "/recommendations")
        require(denied.status == 404, "Hidden source recommendations bypass ACL")
        denied.dispose()
        require(
            recommendation_ids(owner, base_url, target_id) == [],
            "Owner's hidden source has recommendations",
        )
        visible = mutation(
            admin,
            base_url,
            "PATCH",
            moderation_path,
            {"status": "visible", "reason": "QA: восстановление тестового видео"},
        )
        require(visible.status == 200, "Visible recommendation fixture restore failed")
        visible.dispose()
        report = {
            "filtering": "Current/private/non-READY/hidden excluded; source ACL passed",
            "autoplay": autoplay_experience(
                page, base_url, source_id, target, artifacts
            ),
        }
        return report
    except BaseException:
        page.screenshot(
            path=artifacts / "experience-autoplay-failure.png", full_page=True
        )
        raise
    finally:
        # Release only fixtures created above; never touch the runner's original video.
        for video_id in (reserve_id, target["id"] if target else None):
            if video_id:
                deleted = mutation(
                    owner, base_url, "DELETE", f"/api/v1/videos/{video_id}", None
                )
                require(deleted.status == 204, "Recommendation fixture cleanup failed")
                deleted.dispose()
        admin_context.close()
        guest.close()
