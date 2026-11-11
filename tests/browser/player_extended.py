"""Extended player checks on the disposable stack with real H.264 playback."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from player import no_overflow, ready, require
from playwright.sync_api import Browser, Page, expect


def wait_elapsed(page: Page, milliseconds: int) -> None:
    started = page.evaluate("() => performance.now()")
    page.wait_for_function(
        "args => performance.now() - args.started >= args.milliseconds",
        arg={"started": started, "milliseconds": milliseconds},
    )


def check_mouse_clicks(page: Page) -> str:
    """Use native Chrome mouse events, including a held second mouse press."""
    video = page.locator(".video-player")
    root = page.locator("[data-fox-player]")
    for delay in [0, 350]:
        video.dblclick(position={"x": 30, "y": 30}, delay=delay)
        page.wait_for_function("() => Boolean(document.fullscreenElement)")
        wait_elapsed(page, 600)
        require(video.evaluate("v => v.paused"), "Double click also started playback")
        page.locator("[data-fox-fullscreen]").click()
        page.wait_for_function("() => !document.fullscreenElement")

    video.click(position={"x": 30, "y": 30})
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return !v.paused && v.currentTime > .2; }"
    )
    playing_position = video.evaluate("v => v.currentTime")
    video.dblclick(position={"x": 30, "y": 30}, delay=350)
    page.wait_for_function("() => Boolean(document.fullscreenElement)")
    wait_elapsed(page, 600)
    require(
        video.evaluate(
            "(v, time) => !v.paused && v.currentTime > time", playing_position
        ),
        "Double click interrupted actual playback",
    )
    page.locator("[data-fox-fullscreen]").click()
    page.wait_for_function("() => !document.fullscreenElement")
    video.click(position={"x": 30, "y": 30})
    page.wait_for_function("() => document.querySelector('.video-player').paused")
    root.focus()
    root.press("Home")
    return "native single click; double click at 0/350ms preserves paused/playing state"


def check_player_extended(
    browser: Browser,
    page: Page,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    page.goto(base_url + watch_path)
    ready(page)
    root = page.locator("[data-fox-player]")
    video = page.locator(".video-player")
    settings = page.locator("[data-fox-settings]")
    panel = page.locator("[data-fox-settings-panel]")
    speed = page.locator("[data-fox-speed]")
    loop = page.locator("[data-fox-loop]")
    quality = page.locator("[data-fox-quality]")
    captions = page.locator("[data-fox-captions]")
    theater = page.locator("[data-fox-theater]")

    # One published MP4 must not offer fictional transcoded resolutions.
    settings.click()
    expect(settings).to_have_attribute("aria-expanded", "true")
    expect(panel).to_be_visible()
    expect(quality).to_be_disabled()
    require(quality.locator("option").count() == 1, "Invented quality variants")
    require(
        str(video.evaluate("v => v.videoHeight")) in quality.inner_text(),
        "Quality selector does not describe the actual decoded video",
    )
    expect(captions).to_be_disabled()
    panel.press("Escape")
    expect(panel).to_be_hidden()
    expect(settings).to_be_focused()

    # Settings must change actual media and survive navigation without autoplay.
    settings.click()
    speed.select_option("1.5")
    loop.check()
    panel.press("Escape")
    volume = page.locator("[data-fox-volume]")
    volume.focus()
    volume.press("Home")
    for _ in range(7):
        volume.press("ArrowRight")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.video-player').volume - .35) < .01"
    )
    page.locator("[data-fox-mute]").click()
    settings.click()
    theater.click()
    expect(theater).to_have_attribute("aria-pressed", "true")
    panel.press("Escape")
    page.reload()
    ready(page)
    require(
        video.evaluate(
            "v => v.paused && v.muted && v.loop && v.playbackRate === 1.5 && "
            "Math.abs(v.volume - .35) < .01"
        ),
        "Reload lost preferences or started playback",
    )
    expect(theater).to_have_attribute("aria-pressed", "true")
    require(
        root.evaluate("r => r.classList.contains('is-theater')"),
        "Saved theater setting does not affect the layout",
    )
    page.locator("[data-fox-mute]").click()
    require(
        video.evaluate("v => !v.muted && Math.abs(v.volume - .35) < .01"),
        "Unmute did not restore the selected volume",
    )

    settings.click()
    speed.select_option("1")
    panel.press("Escape")
    root.focus()
    root.press("5")
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return !v.seeking && Math.abs(v.currentTime - v.duration / 2) < .1; }"
    )
    root.press("j")
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime < .1"
    )
    root.press("l")
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return v.currentTime > v.duration - .1; }"
    )
    root.press("Home")
    root.press("ArrowRight")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.video-player').currentTime - 5) < .1"
    )
    root.press(".")
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > 5.02"
    )
    root.press(",")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.video-player').currentTime - 5) < .01"
    )
    root.press("Shift+.")
    require(video.evaluate("v => v.playbackRate === 1.25"), "Speed shortcut failed")
    root.press("Shift+,")
    require(video.evaluate("v => v.playbackRate === 1"), "Speed decrease failed")
    root.press("End")
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return v.currentTime > v.duration - .1; }"
    )
    root.press("Home")

    double_click = check_mouse_clicks(page)

    page.locator("[data-fox-center]").click()
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > .3"
    )
    settings.click()
    page.mouse.move(0, 0)
    wait_elapsed(page, 3000)
    expect(panel).to_be_visible()
    expect(page.locator("[data-fox-controls]")).to_have_css("opacity", "1")
    require(video.evaluate("v => !v.paused"), "Open settings paused playback")
    panel.press("Escape")
    expect(settings).to_be_focused()
    page.locator("[data-fox-toggle]").click()

    # Native WebVTT cues must be usable and must not persist a local file URL.
    settings.click()
    page.locator("[data-fox-caption-file]").set_input_files(
        {
            "name": "player-check.vtt",
            "mimeType": "text/vtt",
            "buffer": (
                "WEBVTT\n\n00:00:00.000 --> 00:00:09.000\n"
                "Проверка настоящих субтитров\n"
            ).encode(),
        }
    )
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return [...v.textTracks].some(t => t.mode === 'showing' && "
        "t.cues?.length === 1); }"
    )
    expect(captions).to_be_enabled()
    for name, content, feedback in [
        ("invalid.vtt", b"This is not WebVTT", "Не удалось прочитать WebVTT"),
        ("empty.vtt", b"WEBVTT\n\n", "В файле нет читаемых субтитров"),
        ("large.vtt", b"WEBVTT\n\n" + b"x" * (1024 * 1024), "до 1 МиБ"),
    ]:
        page.locator("[data-fox-caption-file]").set_input_files(
            {"name": name, "mimeType": "text/vtt", "buffer": content}
        )
        expect(page.locator("[data-fox-feedback]")).to_contain_text(feedback)
        require(
            video.evaluate(
                "v => [...v.textTracks].some(t => t.mode === 'showing' && "
                "t.cues?.length === 1 && "
                "t.cues[0].text === 'Проверка настоящих субтитров')"
            ),
            "Rejected caption replacement removed the working subtitle track",
        )
    panel.press("Escape")
    root.focus()
    root.press("Home")
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return [...v.textTracks].some(t => t.activeCues?.length === 1); }"
    )
    root.press("c")
    require(
        video.evaluate("v => [...v.textTracks].every(t => t.mode !== 'showing')"),
        "Caption shortcut did not disable the native track",
    )
    root.press("c")
    page.wait_for_function(
        "() => [...document.querySelector('.video-player').textTracks]"
        ".some(t => t.mode === 'showing')"
    )
    page.screenshot(path=artifacts / "player-extended-captions.png", full_page=True)
    require(
        page.evaluate(
            "() => !localStorage.getItem('foxtube.player.v1').includes('blob:')"
        ),
        "Local caption URL leaked into persistent preferences",
    )
    page.reload()
    ready(page)
    expect(captions).to_be_disabled()
    require(video.evaluate("v => v.textTracks.length === 0"), "Local file persisted")

    # Restore defaults for later application scenarios on the same context.
    settings.click()
    speed.select_option("1")
    loop.uncheck()
    if theater.get_attribute("aria-pressed") == "true":
        theater.click()
    panel.press("Escape")
    root.focus()
    root.press("Home")
    no_overflow(page)
    page.screenshot(path=artifacts / "player-extended-desktop.png", full_page=True)

    mobile_report = check_mobile(browser, base_url, watch_path, artifacts, errors)
    storage_report = check_unavailable_storage(browser, base_url, watch_path, errors)
    switching_report = check_source_switching(
        browser, base_url, watch_path, errors, artifacts
    )
    return {
        "quality": "single actual decoded resolution; no fictional alternatives",
        "preferences": "rate, volume, mute, loop and theater survived reload",
        "keyboard": "J/L, digits, Home/End, paused frames and speed passed",
        "double_click": double_click,
        "settings": "Escape restores focus; open panel stays visible during playback",
        "captions": (
            "real WebVTT cues, shortcut, session-only file; invalid/empty/large "
            "replacement preserves working track"
        ),
        "mobile": mobile_report,
        "unavailable_storage": storage_report,
        "source_switching_fixture": switching_report,
    }


def check_source_switching(
    browser: Browser,
    base_url: str,
    watch_path: str,
    errors: list[str],
    artifacts: Path,
) -> dict[str, str]:
    """Exercise source reloads using two URLs for the same real protected MP4."""
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    diagnostics: dict[str, Any] = {"responses": [], "request_failures": []}

    def resource_kind(url: str) -> str:
        path = urlsplit(url).path
        if path.startswith("/static/"):
            return "static/" + Path(path).name
        if path.startswith("/media/"):
            return "media"
        return "other"

    # Keep the real HTTP document and its IP address space. A fulfilled synthetic
    # document on a private HTTP origin can trigger Chrome Local Network Access
    # restrictions for every subsequent subresource. Patch during parsing, before
    # the deferred player script takes its snapshot of the available sources.
    context.add_init_script(
        """(() => {
          const addSource = () => {
            const source = document.querySelector('.video-player source[src]');
            if (!source) return;
            observer.disconnect();
            const second = source.cloneNode();
            const url = new URL(source.src);
            url.searchParams.set('player_variant', 'second');
            source.dataset.quality = 'Вариант A';
            second.src = url.href;
            second.dataset.quality = 'Вариант B';
            source.after(second);
          };
          const observer = new MutationObserver(addSource);
          observer.observe(document, {childList: true, subtree: true});
          addSource();
        })();"""
    )

    try:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on(
            "response",
            lambda response: diagnostics["responses"].append(
                {"kind": resource_kind(response.url), "status": response.status}
            ),
        )
        page.on(
            "requestfailed",
            lambda request: diagnostics["request_failures"].append(
                {
                    "kind": resource_kind(request.url),
                    "failure": request.failure,
                }
            ),
        )
        page.goto(base_url + watch_path)
        try:
            ready(page)
        except Exception:
            # Never save HTML, URLs, cookies or headers. Diagnostics must not
            # replace the original failure if the page is already unavailable.
            try:
                diagnostics["state"] = page.evaluate(
                    """() => {
                  const v = document.querySelector('.video-player');
                  return {
                    hasVideo: Boolean(v), readyState: v?.readyState,
                    networkState: v?.networkState, errorCode: v?.error?.code,
                    paused: v?.paused,
                    sourceCount: v?.querySelectorAll('source').length,
                    enhanced: document.querySelector('[data-fox-player]')
                      ?.classList.contains('is-enhanced')
                  };
                }"""
                )
                (artifacts / "source-switch-failure.json").write_text(
                    json.dumps(diagnostics, indent=2) + "\n"
                )
            except Exception:
                pass
            raise
        video = page.locator(".video-player")
        root = page.locator("[data-fox-player]")
        settings = page.locator("[data-fox-settings]")
        panel = page.locator("[data-fox-settings-panel]")
        quality = page.locator("[data-fox-quality]")
        settings.click()
        expect(quality).to_be_enabled()
        expect(quality.locator("option")).to_have_text(["Вариант A", "Вариант B"])
        page.locator("[data-fox-speed]").select_option("1.5")
        panel.press("Escape")
        volume = page.locator("[data-fox-volume]")
        volume.focus()
        volume.press("Home")
        for _ in range(8):
            volume.press("ArrowRight")
        root.focus()
        root.press("5")
        page.wait_for_function(
            "() => { const v = document.querySelector('.video-player'); "
            "return !v.seeking && Math.abs(v.currentTime - v.duration / 2) < .1; }"
        )
        paused_position = video.evaluate("v => v.currentTime")
        settings.click()
        quality.select_option("1")
        page.wait_for_function(
            "position => { const v = document.querySelector('.video-player'); "
            "return v.readyState >= 2 && !v.seeking && v.paused && "
            "v.currentSrc.includes('player_variant=second') && "
            "Math.abs(v.currentTime - position) < .15 && v.playbackRate === 1.5 && "
            "Math.abs(v.volume - .4) < .01 && !v.muted && !v.error; }",
            arg=paused_position,
        )
        panel.press("Escape")
        root.focus()
        root.press("Home")
        page.locator("[data-fox-center]").click()
        page.wait_for_function(
            "() => document.querySelector('.video-player').currentTime > .5"
        )
        settings.click()
        playing_position = video.evaluate("v => v.currentTime")
        quality.select_option("0")
        page.wait_for_function(
            "position => { const v = document.querySelector('.video-player'); "
            "return v.readyState >= 2 && !v.seeking && !v.paused && "
            "!v.currentSrc.includes('player_variant=second') && "
            "v.currentTime >= position - .15 && v.playbackRate === 1.5 && "
            "Math.abs(v.volume - .4) < .01 && !v.muted && !v.error; }",
            arg=playing_position,
        )
        resumed_position = video.evaluate("v => v.currentTime")
        page.wait_for_function(
            "position => document.querySelector('.video-player').currentTime > "
            "position + .4",
            arg=resumed_position,
        )
        return {
            "fixture": (
                "two same-origin protected URLs for the same real H.264 file; "
                "this checks source switching, not multiple resolutions"
            ),
            "paused": "position, rate, volume and pause retained after source reload",
            "playing": "position, rate and volume retained; real playback resumed",
        }
    finally:
        context.close()


def check_touch_double_tap(page: Page) -> str:
    settings = page.locator("[data-fox-settings]")
    settings.tap()
    page.locator("[data-fox-loop]").uncheck()
    settings.tap()
    root = page.locator("[data-fox-player]")
    video = page.locator(".video-player")
    root.focus()
    root.press("5")
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return v.paused && !v.seeking && Math.abs(v.currentTime - 5) < .1; }"
    )
    video.scroll_into_view_if_needed()
    bounds = video.bounding_box()
    require(bounds is not None, "Touch fixture could not locate the real video")
    y = bounds["y"] + bounds["height"] * 0.25
    # Separate this gesture from the preceding single tap that revealed controls.
    wait_elapsed(page, 400)
    for fraction, at_end in [(0.2, False), (0.8, True)]:
        x = bounds["x"] + bounds["width"] * fraction
        page.touchscreen.tap(x, y)
        page.touchscreen.tap(x, y)
        page.wait_for_function(
            "end => { const v = document.querySelector('.video-player'); "
            "return !v.seeking && v.paused && (end ? "
            "v.currentTime > v.duration - .1 : v.currentTime < .1); }",
            arg=at_end,
        )
        wait_elapsed(page, 400)
        require(video.evaluate("v => v.paused"), "Double tap also toggled playback")
    root.focus()
    root.press("Home")
    page.locator("[data-fox-center]").tap()
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > .4"
    )
    root.focus()
    root.press("5")
    page.touchscreen.tap(bounds["x"] + bounds["width"] * 0.2, y)
    page.touchscreen.tap(bounds["x"] + bounds["width"] * 0.2, y)
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-player'); "
        "return !v.paused && !v.seeking && v.currentTime > .2 && v.currentTime < 3; }"
    )
    page.locator("[data-fox-toggle]").tap()
    return "native touch events: ±10s clamp at start/end; no extra pause/play toggle"


def check_unavailable_storage(
    browser: Browser,
    base_url: str,
    watch_path: str,
    errors: list[str],
) -> str:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.add_init_script(
        "Object.defineProperty(window, 'localStorage', { get() { "
        "throw new DOMException('Storage disabled for browser test', 'SecurityError'); "
        "} });"
    )
    try:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(base_url + watch_path)
        ready(page)
        settings = page.locator("[data-fox-settings]")
        settings.click()
        page.locator("[data-fox-speed]").select_option("1.25")
        page.locator("[data-fox-loop]").check()
        page.locator("[data-fox-settings-panel]").press("Escape")
        page.locator("[data-fox-mute]").click()
        require(
            page.locator(".video-player").evaluate(
                "v => v.paused && v.muted && v.loop && v.playbackRate === 1.25"
            ),
            "Unavailable storage prevented player settings from working",
        )
        page.locator("[data-fox-center]").click()
        page.wait_for_function(
            "() => document.querySelector('.video-player').currentTime > .4"
        )
        page.locator("[data-fox-toggle]").click()
        require(
            page.locator(".video-player").evaluate("v => v.paused"),
            "Unavailable storage prevented pausing real media",
        )
        return "controlled SecurityError on localStorage; settings and playback work"
    finally:
        context.close()


def check_mobile(
    browser: Browser,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, str]:
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        locale="ru-RU",
    )
    try:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(base_url + watch_path)
        ready(page)
        settings = page.locator("[data-fox-settings]")
        panel = page.locator("[data-fox-settings-panel]")
        for width, height in [(320, 740), (390, 844), (844, 390)]:
            page.set_viewport_size({"width": width, "height": height})
            no_overflow(page)
            settings.tap()
            expect(panel).to_be_visible()
            require(
                panel.evaluate(
                    "p => { const r = p.getBoundingClientRect(); "
                    "return r.left >= 0 && r.right <= innerWidth && "
                    "r.width > 0 && r.height > 0; }"
                ),
                "Mobile settings extend outside the viewport",
            )
            page.screenshot(
                path=artifacts / f"player-extended-mobile-{width}.png", full_page=True
            )
            settings.tap()

        page.set_viewport_size({"width": 390, "height": 844})
        settings.tap()
        page.locator("[data-fox-loop]").check()
        settings.tap()
        page.locator("[data-fox-center]").tap()
        page.wait_for_function(
            "() => document.querySelector('.video-player').currentTime > .4"
        )
        page.wait_for_function(
            "() => document.querySelector('[data-fox-player]')"
            ".classList.contains('is-controls-hidden')"
        )
        page.locator(".video-player").tap(position={"x": 25, "y": 25})
        expect(page.locator("[data-fox-controls]")).to_have_css("opacity", "1")
        require(
            page.locator(".video-player").evaluate("v => !v.paused"),
            "First touch to reveal hidden controls unexpectedly paused playback",
        )
        page.locator("[data-fox-toggle]").tap()
        require(
            page.locator(".video-player").evaluate("v => v.paused"),
            "Touch pause did not affect real playback",
        )
        double_tap = check_touch_double_tap(page)
        return {
            "viewports": "320x740, 390x844, 844x390: settings inside viewport",
            "touch": "hidden controls reveal without pause; explicit pause works",
            "double_tap": double_tap,
            "scope": "Chrome mobile emulation; physical iOS/Android not tested",
        }
    finally:
        context.close()
