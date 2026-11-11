"""Persistent miniplayer checks on real Nginx/H.264, without synthetic documents."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from player import no_overflow, ready, require
from player_extended import wait_elapsed
from playwright.sync_api import Browser, Error, Page, Route, expect


def remember_video(page: Page, selector: str = "[data-fox-player] video") -> None:
    page.locator(selector).evaluate(
        """video => {
          window.__miniQA = {video, document, source: video.currentSrc,
            time: video.currentTime, loads: 0};
          video.addEventListener('loadstart', () => window.__miniQA.loads++);
        }"""
    )


def same_video(page: Page) -> None:
    require(
        page.evaluate(
            """() => {
              const q = window.__miniQA;
              return q && q.document === document && q.video.isConnected &&
                document.querySelector('[data-fox-player] video') === q.video &&
                q.video.currentSrc === q.source && q.loads === 0;
            }"""
        ),
        "Navigation/mini transition replaced the video or reloaded its media",
    )
    require(
        page.evaluate(
            """() => [...document.querySelectorAll('video,audio')]
              .filter(v => !v.paused && !v.muted && v.volume > 0).length <= 1"""
        ),
        "More than one media element is producing sound",
    )
    require(
        page.evaluate("() => !window.__miniQaConcurrentAudio"),
        "Concurrent audio was observed during a media transition",
    )


def preserved_state(page: Page) -> None:
    same_video(page)
    require(
        page.evaluate(
            """() => {
              const v = window.__miniQA.video;
              return v.paused && v.muted && Math.abs(v.volume - .35) < .01 &&
                v.playbackRate === 1.25 && Math.abs(v.currentTime - 5) < .15 &&
                [...v.textTracks].some(t => t.mode === 'showing' &&
                  t.cues?.length === 1 && t.cues[0].text === 'Миниплеер WebVTT');
            }"""
        ),
        "Mini/navigation lost paused position, volume, mute, speed or local captions",
    )


def advance(page: Page, beyond: float) -> float:
    page.wait_for_function(
        "time => { const v = window.__miniQA.video; "
        "return !v.paused && !v.error && v.currentTime > time; }",
        arg=beyond,
        timeout=20000,
    )
    return float(page.evaluate("() => window.__miniQA.video.currentTime"))


def mini_visible(page: Page) -> None:
    expect(page.locator("[data-miniplayer]")).to_be_visible()
    expect(page.locator("[data-miniplayer] [data-fox-player]")).to_be_visible()
    for name in ["toggle", "mute", "captions", "settings"]:
        expect(page.locator(f"[data-miniplayer] [data-fox-{name}]")).to_be_visible()


def unloaded_mini(page: Page) -> None:
    expect(page.locator("[data-miniplayer]")).to_be_hidden()
    expect(page.locator("[data-mini-title]")).to_have_text("")
    page.wait_for_function(
        """() => {
          const v = window.__miniQA.video;
          // Chrome may retain the last selected currentSrc after resource reset.
          return !v.isConnected && v.paused &&
            v.readyState === HTMLMediaElement.HAVE_NOTHING &&
            v.networkState === HTMLMediaElement.NETWORK_EMPTY &&
            !v.hasAttribute('src') && !v.hasAttribute('poster') &&
            !v.querySelector('source,track') && v.buffered.length === 0;
        }"""
    )
    time = page.evaluate("() => window.__miniQA.video.currentTime")
    wait_elapsed(page, 650)
    require(
        page.evaluate(
            "time => window.__miniQA.video.paused && "
            "window.__miniQA.video.currentTime === time",
            time,
        ),
        "Closed mini kept playing",
    )


def close_mini(page: Page) -> None:
    page.locator("[data-mini-close]").click()
    unloaded_mini(page)


def navigation_race(page: Page, base_url: str, watch_path: str) -> str:
    """Hold the first real HTTP request while the second navigation wins."""
    page.goto(base_url + watch_path)
    ready(page)
    remember_video(page)
    page.locator("[data-fox-mini]").click()
    channel = page.locator('.watch-meta strong a[href^="/channels/"]')
    destination = base_url + str(channel.get_attribute("href"))
    name = channel.inner_text()
    delayed: list[Route] = []

    def hold(route: Route) -> None:
        delayed.append(route)
        page.evaluate("() => { window.__miniQaHeldNavigation = true; }")

    first_url = base_url + "/"
    page.route(first_url, hold)
    try:
        page.locator('.brand[href="/"]').click()
        page.wait_for_function("() => window.__miniQaHeldNavigation === true")
        require(len(delayed) == 1, "Expected exactly one delayed HTML request")
        channel.click()
        page.wait_for_url(destination)
        expect(page.locator("#channel-title")).to_have_text(name)
    finally:
        for route in delayed:
            try:
                # Continue the original request; never replace its response body.
                route.continue_()
            except Error:
                require(
                    route.request.failure == "net::ERR_ABORTED",
                    "Delayed navigation failed for a reason other than cancellation",
                )
        page.unroute(first_url, hold)
    wait_elapsed(page, 650)
    require(page.url == destination, "The late first navigation replaced the final URL")
    expect(page.locator("#channel-title")).to_have_text(name)
    same_video(page)
    close_mini(page)
    return "Delayed A resumed/cancelled; B URL/main remain; original HTTP unchanged"


def watch_navigation(page: Page, base_url: str, watch_path: str) -> dict[str, Any]:
    page.goto(base_url + watch_path)
    ready(page)
    require(page.evaluate("() => Boolean(window.FoxMiniPlayer)"), "Mini API absent")
    title = page.locator(".watch-heading h1").inner_text()
    root = page.locator("[data-fox-player]")
    page.locator("[data-fox-settings]").click()
    page.locator("[data-fox-speed]").select_option("1.25")
    page.locator("[data-fox-loop]").uncheck()
    page.locator("[data-fox-caption-file]").set_input_files(
        {
            "name": "mini-check.vtt",
            "mimeType": "text/vtt",
            "buffer": (
                "WEBVTT\n\n00:00:00.000 --> 00:00:09.900\nМиниплеер WebVTT\n"
            ).encode(),
        }
    )
    page.wait_for_function(
        "() => [...document.querySelector('video').textTracks]"
        ".some(t => t.mode === 'showing' && t.cues?.length === 1)"
    )
    page.locator("[data-fox-settings-panel]").press("Escape")
    volume = page.locator("[data-fox-volume]")
    volume.focus()
    volume.press("Home")
    for _ in range(7):
        volume.press("ArrowRight")
    page.locator("[data-fox-mute]").click()
    root.focus()
    root.press("5")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('video').currentTime - 5) < .15"
    )
    remember_video(page)
    preserved_state(page)
    page.locator("[data-fox-mini]").click()
    mini_visible(page)
    expect(page.locator("[data-mini-title]")).to_have_text(title)
    preserved_state(page)
    expect(volume).to_be_visible()
    volume.focus()
    volume.press("Home")
    for _ in range(8):
        volume.press("ArrowRight")
    page.wait_for_function("() => Math.abs(window.__miniQA.video.volume - .4) < .01")
    volume.press("ArrowLeft")
    page.wait_for_function("() => Math.abs(window.__miniQA.video.volume - .35) < .01")
    # Changing a range deliberately unmutes; restore the tested muted preference.
    page.locator("[data-fox-mute]").click()
    preserved_state(page)

    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    expect(page.locator("#main-content .video-grid")).to_be_visible()
    mini_visible(page)
    preserved_state(page)
    page.locator("#site-search").fill(title)
    page.locator(".search-submit").click()
    page.wait_for_url("**/search?*")
    card = page.locator(f'.video-card h2 a[href="{watch_path}"]')
    expect(card).to_be_visible()
    preserved_state(page)
    page.go_back()
    page.wait_for_url(base_url + "/")
    expect(page.locator("#page-title")).to_have_text("Главная")
    expect(page.locator("#site-search")).to_have_value("")
    expect(page.locator("#main-content .video-grid")).to_be_visible()
    preserved_state(page)
    page.go_forward()
    page.wait_for_url("**/search?*")
    expect(page.locator("#page-title")).to_have_text("Результаты поиска")
    expect(page.locator("#site-search")).to_have_value(title)
    expect(card).to_be_visible()
    preserved_state(page)
    card.click()
    page.wait_for_url(base_url + watch_path)
    expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
    expect(page.locator("[data-miniplayer]")).to_be_hidden()
    preserved_state(page)

    # Dynamically installed social listeners must submit once after repeated nav.
    like = page.locator("[data-like]")
    expect(like).to_be_enabled()
    expect(like).to_have_attribute("aria-pressed", "false")
    mutations: list[str] = []

    def record(request: Any) -> None:
        if urlsplit(request.url).path.endswith("/like"):
            mutations.append(request.method)

    page.on("request", record)
    try:
        like.click()
        expect(like).to_have_attribute("aria-pressed", "true")
        like.click()
        expect(like).to_have_attribute("aria-pressed", "false")
    finally:
        page.remove_listener("request", record)
    require(
        mutations == ["PUT", "DELETE"], "Soft navigation duplicated social handlers"
    )

    page.locator("[data-fox-mini]").click()
    mini_visible(page)
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    page.locator("[data-mini-restore]").click()
    page.wait_for_url(base_url + watch_path)
    expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
    preserved_state(page)

    # Resume with a real user action, seek through the actual controls, then hear it.
    root.focus()
    root.press("7")
    page.wait_for_function(
        "() => Math.abs(window.__miniQA.video.currentTime - 7) < .15 && "
        "!window.__miniQA.video.seeking"
    )
    page.locator("[data-fox-mini]").click()
    page.locator("[data-fox-mute]").click()
    require(page.evaluate("() => !window.__miniQA.video.muted"), "Unmute failed")
    page.locator("[data-fox-toggle]").click()
    played = advance(page, 8)
    same_video(page)
    page.locator("[data-fox-toggle]").click()
    root.focus()
    root.press("Home")
    page.locator("[data-fox-toggle]").click()
    advance(page, 0.3)
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    expect(page.locator("#main-content .video-grid")).to_be_visible()
    advance(page, 1.2)
    mini_visible(page)
    same_video(page)
    close_mini(page)
    return {
        "identity_and_preferences": "passed",
        "local_webvtt": "passed",
        "links_search_history_restore": "passed",
        "social_handlers": "exactly one PUT and DELETE",
        "seek_and_resume_time": played,
        "playing_navigation": "same decoder; actual time advances after main swap",
        "close_unloads_media": "detached; cleared attributes; empty decoder/buffer",
    }


def preview_pinning(page: Page, base_url: str, watch_path: str) -> dict[str, Any]:
    page.goto(base_url + "/")
    thumbnail = page.locator(f'.video-thumbnail[href="{watch_path}"]')
    thumbnail.hover()
    preview = thumbnail.locator("video")
    expect(preview).to_be_visible()
    page.wait_for_function(
        "() => { const v = document.querySelector('.thumbnail-preview'); "
        "return v && v.muted && !v.paused && v.currentTime > .3; }"
    )
    remember_video(page, f'.video-thumbnail[href="{watch_path}"] video')
    # Snapshot in the event immediately before the application's click handler.
    pin = thumbnail.locator("..").locator("[data-preview-mini]")
    pin.evaluate(
        """button => button.addEventListener('click', () => {
          window.__miniQA.time = window.__miniQA.video.currentTime;
        }, {capture: true, once: true})"""
    )
    pin.click()
    mini_visible(page)
    same_video(page)
    require(
        page.evaluate(
            "() => Math.abs(window.__miniQA.video.currentTime - "
            "window.__miniQA.time) < .6"
        ),
        "Pinning reset the actual preview position",
    )
    page.mouse.move(0, 0)
    advance(page, 3)
    same_video(page)
    require(
        page.evaluate("() => window.__miniQA.video.muted"),
        "Preview pinning enabled sound without the user's unmute action",
    )
    unmute = page.locator("[data-fox-unmute]")
    expect(unmute).to_be_visible()
    unmute.click()
    require(page.evaluate("() => !window.__miniQA.video.muted"), "Manual unmute failed")
    expect(unmute).to_be_hidden()
    page.locator("[data-fox-toggle]").click()
    page.wait_for_function("() => window.__miniQA.video.paused")
    page.locator("[data-fox-settings]").click()
    expect(page.locator("[data-fox-speed]")).to_be_visible()
    expect(page.locator("[data-fox-caption-file]")).to_be_visible()
    page.locator("[data-fox-settings-panel]").press("Escape")
    page.locator("[data-mini-restore]").click()
    page.wait_for_url(base_url + watch_path)
    expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
    same_video(page)
    require(
        page.evaluate("() => window.__miniQA.video.paused"),
        "Restoring a paused preview started playback",
    )
    page.locator("[data-fox-mini]").click()
    close_mini(page)
    # A keyboard pin starts without a preceding hover/media selection. Chrome's
    # initial NETWORK_NO_SOURCE must not become a stale visible error banner.
    page.goto(base_url + "/")
    direct = page.locator(f'[data-preview-mini="{watch_path.rsplit("/", 1)[-1]}"]')
    direct.focus()
    direct.press("Enter")
    mini_visible(page)
    page.wait_for_function(
        "() => {const v=document.querySelector('[data-miniplayer] video');"
        "return v?.readyState>=2 && !v.paused && !v.error && v.currentTime>.3;}"
    )
    expect(page.locator("[data-fox-message]")).to_be_hidden()
    remember_video(page)
    close_mini(page)
    return {
        "same_video_and_position": "passed",
        "beyond_preview_loop": "passed",
        "direct_pin_without_hover": "real playback; no false loading failure",
    }


def mobile_layout(page: Page, base_url: str, watch_path: str, artifacts: Path) -> str:
    page.goto(base_url + watch_path)
    ready(page)
    remember_video(page)
    page.locator("[data-fox-mini]").click()
    for width, height in [(320, 740), (390, 844), (844, 390)]:
        page.set_viewport_size({"width": width, "height": height})
        mini_visible(page)
        no_overflow(page)
        require(
            page.locator("[data-miniplayer]").evaluate(
                """mini => {
                  const r = mini.getBoundingClientRect();
                  return r.left >= 0 && r.top >= 0 &&
                    r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1;
                }"""
            ),
            f"Mini player extends beyond {width}x{height} viewport",
        )
        page.locator("[data-fox-settings]").click()
        expect(page.locator("[data-fox-speed]")).to_be_visible()
        page.screenshot(path=artifacts / f"miniplayer-{width}x{height}.png")
        page.locator("[data-fox-settings-panel]").press("Escape")
        same_video(page)
    volume = page.locator("[data-fox-volume]")
    expect(volume).to_be_visible()
    volume.focus()
    volume.press("Home")
    for _ in range(5):
        volume.press("ArrowRight")
    page.wait_for_function("() => Math.abs(window.__miniQA.video.volume - .25) < .01")
    page.locator("[data-fox-mute]").tap()
    require(page.evaluate("() => window.__miniQA.video.muted"), "Touch mute failed")
    page.locator("[data-fox-toggle]").tap()
    advance(page, 0.3)
    page.locator("[data-fox-mute]").tap()
    require(page.evaluate("() => !window.__miniQA.video.muted"), "Touch unmute failed")
    close_mini(page)
    page.goto(base_url + "/")
    page.locator(f'[data-preview-mini="{watch_path.rsplit("/", 1)[-1]}"]').tap()
    page.wait_for_function(
        "() => {const v=document.querySelector('[data-miniplayer] video');"
        "return v?.readyState>=2 && v.muted && !v.paused && !v.error && "
        "v.currentTime>.3;}"
    )
    expect(page.locator("[data-fox-message]")).to_be_hidden()
    remember_video(page)
    close_mini(page)
    return "320x740, 390x844, 844x390; controls/settings fit; touch context"


def login_after_navigation(
    page: Page, base_url: str, watch_path: str, email: str, password: str
) -> dict[str, str]:
    page.goto(base_url + watch_path)
    ready(page)
    remember_video(page)
    page.locator("[data-fox-mini]").click()
    page.locator("[data-fox-toggle]").click()
    advance(page, 0.3)
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    expect(page.locator("#main-content .video-grid")).to_be_visible()
    same_video(page)
    page.locator('.login-link[href="/login"]').click()
    page.wait_for_url(base_url + "/login")
    # Session-changing screens intentionally use native navigation and unload mini.
    expect(page.locator("[data-miniplayer]")).to_be_hidden()
    expect(page.locator("video")).to_have_count(0)
    page.get_by_label("Email", exact=True).fill(email)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Войти", exact=True).click()
    page.wait_for_url(base_url + "/account")
    expect(page.locator('[data-auth-form="logout"]')).to_be_visible()
    expect(page.locator(".login-link")).to_have_count(0)

    # This context acquired its own new session through real login above. Never
    # log out an owner storage-state clone: that would revoke the owner's session.
    page.goto(base_url + watch_path)
    ready(page)
    remember_video(page)
    page.locator("[data-fox-mini]").click()
    page.locator("[data-fox-toggle]").click()
    advance(page, 0.3)
    page.locator('.brand[href="/"]').click()
    page.wait_for_url(base_url + "/")
    expect(page.locator("#page-title")).to_have_text("Главная")
    same_video(page)
    other = page.context.new_page()
    try:
        other.goto(base_url + "/account")
        other.locator('form[data-auth-form="logout"] button').click()
        other.wait_for_url(base_url + "/")
        expect(other.locator('.login-link[href="/login"]')).to_be_visible()
        page.bring_to_front()
        unloaded_mini(page)
        # A new request must render the anonymous shell, not the old account.
        page.locator('.primary-nav a[href="/search"]').click()
        page.wait_for_url(base_url + "/search")
        expect(page.locator('.login-link[href="/login"]')).to_be_visible()
        expect(page.locator('[data-auth-form="logout"]')).to_have_count(0)
        expect(page.locator("[data-miniplayer]")).to_be_hidden()
    finally:
        other.close()
    return {
        "login": "Soft nav then native auth boundary closes mini; real login submitted",
        "session_change": "Other-tab logout clears media attributes/decoder/title",
    }


def check_miniplayer(
    browser: Browser,
    owner_page: Page,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
    owner_email: str,
    owner_password: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "failed", "requirement": "A1/A2/A4 mini"}
    # Auth data stays in memory; no storage-state files or account values in reports.
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
        storage_state=owner_page.context.storage_state(),
    )
    mobile = browser.new_context(
        viewport={"width": 390, "height": 844},
        locale="ru-RU",
        is_mobile=True,
        has_touch=True,
    )
    guest = browser.new_context(locale="ru-RU")
    for current_context in (context, mobile, guest):
        current_context.add_init_script(
            """(() => {
              window.__miniQaConcurrentAudio = false;
              const check = () => {
                if ([...document.querySelectorAll('video,audio')]
                  .filter(v => !v.paused && !v.muted && v.volume > 0).length > 1)
                  window.__miniQaConcurrentAudio = true;
              };
              for (const event of ['play', 'playing', 'volumechange'])
                document.addEventListener(event, check, true);
            })();"""
        )
    page = context.new_page()
    try:
        for scenario, current_context in [
            ("watch", context),
            ("mobile", mobile),
            ("login", guest),
        ]:
            if scenario != "watch":
                page = current_context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            page.on(
                "console",
                lambda message: (
                    errors.append("miniplayer:console:error")
                    if message.type == "error"
                    else None
                ),
            )
            report["phase"] = scenario
            if scenario == "watch":
                report["watch_navigation"] = watch_navigation(
                    page, base_url, watch_path
                )
                report["phase"] = "preview"
                report["preview"] = preview_pinning(page, base_url, watch_path)
                report["phase"] = "navigation_race"
                report["navigation_race"] = navigation_race(page, base_url, watch_path)
            elif scenario == "mobile":
                report["mobile"] = mobile_layout(page, base_url, watch_path, artifacts)
            else:
                report["login"] = login_after_navigation(
                    page, base_url, watch_path, owner_email, owner_password
                )
        report["status"] = "passed"
        return report
    finally:
        if report["status"] != "passed":
            try:
                report["media"] = page.evaluate(
                    """() => {
                      const v = window.__miniQA?.video;
                      return {connected: v?.isConnected, time: v?.currentTime,
                        paused: v?.paused, muted: v?.muted, ready: v?.readyState,
                        network: v?.networkState, error: v?.error?.code,
                        videos: document.querySelectorAll('video').length};
                    }"""
                )
                page.screenshot(
                    path=artifacts / "miniplayer-failure.png",
                    mask=[page.locator('input[type="password"]')],
                )
            except Exception as error:
                report["diagnostic_error"] = type(error).__name__
        (artifacts / "miniplayer.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        context.close()
        mobile.close()
        guest.close()
