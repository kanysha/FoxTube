"""Player and hover preview behavior on the disposable real-media stack."""

import json
import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, Route, expect


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def ready(page: Page) -> None:
    page.wait_for_function(
        """() => {
          const video = document.querySelector('.video-player');
          return video && video.readyState >= 2 && !video.error;
        }""",
        timeout=30000,
    )
    require(
        page.locator("[data-fox-player]").evaluate(
            "r => r.classList.contains('is-enhanced')"
        ),
        "Player did not initialize custom controls",
    )


def no_overflow(page: Page) -> None:
    require(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        ),
        "Player page has horizontal overflow",
    )


def check_center_logo(page: Page) -> None:
    center = page.locator("[data-fox-center]")
    expect(center).to_be_visible()
    expect(center.locator(":scope > img")).to_have_attribute("src", "/static/logo.svg")
    require(
        center.evaluate(
            """button => {
              const image = button.firstElementChild;
              const style = getComputedStyle(button);
              const target = button.getBoundingClientRect();
              const logo = image.getBoundingClientRect();
              return button.childElementCount === 1 && image.tagName === 'IMG' &&
                image.complete && image.naturalWidth > 0 &&
                Math.abs(logo.width / logo.height -
                  image.naturalWidth / image.naturalHeight) < 0.01 &&
                target.width >= 44 && target.height >= 44 &&
                style.backgroundColor === 'rgba(0, 0, 0, 0)' &&
                style.backgroundImage === 'none' && style.boxShadow === 'none' &&
                style.borderTopWidth === '0px';
            }"""
        ),
        "Central play button changed the original logo or added a backing",
    )


def playback_state(page: Page) -> dict[str, Any]:
    """Collect only media diagnostics, without URLs, text, or account data."""
    try:
        return page.evaluate(
            """() => {
              const video = document.querySelector('.video-player');
              const center = document.querySelector('[data-fox-center]');
              const seek = document.querySelector('[data-fox-seek]');
              return {
                fullscreen: Boolean(document.fullscreenElement),
                current: video?.currentTime,
                duration: video?.duration,
                paused: video?.paused,
                ended: video?.ended,
                seeking: video?.seeking,
                ready: video?.readyState,
                media_error: video?.error?.code,
                center_hidden: center?.hidden,
                seek_value: seek?.value,
              };
            }"""
        )
    except BaseException as error:
        return {"diagnostic_error": type(error).__name__}


def record_failure(
    path: Path, error: BaseException, page: Page, state_key: str
) -> None:
    # Diagnostics must never replace the original browser failure.
    try:
        progress = json.loads(path.read_text()) if path.exists() else {}
        progress[state_key] = playback_state(page)
        progress["failure_type"] = type(error).__name__
        progress["traceback"] = [
            {
                "file": Path(frame.filename).name,
                "line": frame.lineno,
                "function": frame.name,
            }
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        path.write_text(json.dumps(progress) + "\n")
    except BaseException:
        pass


def check_animation(page: Page) -> None:
    loader = page.locator("[data-fox-loading] .fox-loader")

    def decode_background() -> None:
        loader.evaluate(
            """async element => {
              const src = getComputedStyle(element).backgroundImage.slice(5, -2);
              const image = new Image();
              image.src = src;
              await image.decode();
            }"""
        )

    decode_background()
    first = loader.screenshot()
    started = page.evaluate("() => performance.now()")
    page.wait_for_function("started => performance.now() - started > 300", arg=started)
    require(first != loader.screenshot(), "Loader animation stayed still under CSP")
    page.emulate_media(reduced_motion="reduce")
    decode_background()
    first = loader.screenshot()
    started = page.evaluate("() => performance.now()")
    page.wait_for_function("started => performance.now() - started > 300", arg=started)
    require(first == loader.screenshot(), "Reduced-motion loader still animates")
    page.emulate_media(reduced_motion="no-preference")


def check_request_loaders(page: Page, base_url: str) -> dict[str, str]:
    request_page = page.context.new_page()
    try:
        request_page.goto(page.url)
        ready(request_page)
        return _check_request_loaders(request_page, base_url)
    finally:
        request_page.close()


def _check_request_loaders(page: Page, base_url: str) -> dict[str, str]:
    held: list[Route] = []
    pattern = "**/api/v1/videos/*"
    page.route(pattern, lambda route: held.append(route))
    page.locator("[data-video-edit] button[type=submit]").click()
    expect(page.locator("[data-video-edit]")).to_have_attribute("aria-busy", "true")
    expect(page.locator("[data-video-edit] button[type=submit]")).to_be_disabled()
    background = page.locator("[data-video-edit] button[type=submit]").evaluate(
        "b => getComputedStyle(b, '::after').backgroundImage"
    )
    require("foxtube-pulse" in background, "Pending request has no FoxTube indicator")
    require(len(held) == 1, "Request loader fixture did not hold metadata PATCH")
    held[0].fulfill(
        status=422,
        content_type="application/json",
        body='{"detail":"Проверка ошибки запроса"}',
    )
    page.unroute(pattern)
    expect(page.locator("[data-video-edit]")).to_have_attribute("aria-busy", "false")
    expect(page.locator("[data-video-edit] button[type=submit]")).to_be_enabled()

    held.clear()
    pattern = "**/search?*"
    page.route(pattern, lambda route: held.append(route))
    page.locator("#site-search").fill("проверка индикатора")
    # Locator/evaluate calls wait for the pending document navigation to commit.
    # Observe the old document's actual visibility before releasing its request.
    page.evaluate(
        """() => document.addEventListener('submit', () => {
          window.setTimeout(() => {
            const indicator = document.querySelector('[data-navigation-loading]');
            const style = getComputedStyle(indicator);
            const rect = indicator.getBoundingClientRect();
            const visible = !indicator.hidden && style.display !== 'none' &&
              style.visibility === 'visible' && Number(style.opacity) > 0 &&
              rect.width > 0 && rect.height > 0 && rect.bottom > 0 &&
              rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
            console.info(`foxtube-nav-visible:${visible}`);
          }, 500);
        }, {once: true})"""
    )
    with page.expect_console_message(
        predicate=lambda message: message.text.startswith("foxtube-nav-visible:"),
        timeout=5000,
    ) as observed:
        page.locator(".search-submit").click(no_wait_after=True)
    require(
        observed.value.text == "foxtube-nav-visible:true",
        "Navigation indicator was not visible while the real document was held",
    )
    require(len(held) == 1, "Navigation fixture did not hold the document request")
    held[0].continue_()
    page.unroute(pattern)
    page.wait_for_url(base_url + "/search?*")
    expect(page.locator("[data-navigation-loading]")).to_be_hidden()
    return {
        "metadata_request": "indicator while PATCH held; cleared on controlled 422",
        "navigation": "orbit while document held; cleared on destination page",
    }


def check_player(
    browser: Browser,
    page: Page,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    try:
        return _check_player(browser, page, base_url, watch_path, artifacts, errors)
    except BaseException as error:
        record_failure(
            artifacts / "player-progress.json", error, page, "main_playback_state"
        )
        raise


def _check_player(
    browser: Browser,
    page: Page,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    def mark(stage: str) -> None:
        (artifacts / "player-progress.json").write_text(
            json.dumps({"stage": stage}) + "\n"
        )

    mark("desktop-ui")
    page.goto(base_url + watch_path)
    ready(page)
    video = page.locator(".video-player")
    root = page.locator("[data-fox-player]")
    require(video.evaluate("v => v.paused && !v.controls"), "Player autoplayed")
    center = page.locator("[data-fox-center]")
    check_center_logo(page)
    page.screenshot(path=artifacts / "player-desktop.png", full_page=True)
    center.click()
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > 0.7"
    )
    expect(center).to_be_hidden()
    toggle = page.locator("[data-fox-toggle]")
    toggle.click()
    require(video.evaluate("v => v.paused"), "UI pause did not pause video")

    seek = page.locator("[data-fox-seek]")
    seek.click(position={"x": seek.bounding_box()["width"] * 0.7, "y": 9})
    page.wait_for_function(
        """() => {
          const v = document.querySelector('.video-player');
          return !v.seeking && Math.abs(v.currentTime - 7) < 0.7;
        }"""
    )
    mouse_seek = video.evaluate("v => v.currentTime")
    root.focus()
    root.press("ArrowLeft")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.video-player').currentTime - 2) < 0.7"
    )
    root.press("ArrowRight")
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.video-player').currentTime - 7) < 0.7"
    )
    root.press("k")
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > 7.6"
    )
    root.press("Space")
    require(video.evaluate("v => v.paused"), "Space did not pause exactly once")
    mute = page.locator("[data-fox-mute]")
    mute.click()
    require(video.evaluate("v => v.muted"), "Mute button failed")
    expect(mute).to_have_attribute("aria-pressed", "true")
    root.focus()
    root.press("m")
    require(video.evaluate("v => !v.muted"), "Keyboard mute failed")
    volume = page.locator("[data-fox-volume]")
    volume.click(position={"x": volume.bounding_box()["width"] * 0.5, "y": 10})
    require(
        video.evaluate("v => v.volume >= 0.35 && v.volume <= 0.65 && !v.muted"),
        "Volume slider did not affect real playback volume",
    )
    before_form = video.evaluate("v => v.currentTime")
    page.get_by_label("Название", exact=True).fill("k m f")
    require(
        video.evaluate("v => v.paused && !v.muted"),
        "Typing in metadata triggered player keyboard shortcuts",
    )
    require(
        abs(video.evaluate("v => v.currentTime") - before_form) < 0.1,
        "Typing changed playback position",
    )

    mark("fullscreen-enter")
    fullscreen = page.locator("[data-fox-fullscreen]")
    require(
        page.evaluate("() => document.fullscreenEnabled"),
        "Test Chrome must support fullscreen",
    )
    fullscreen.click()
    page.wait_for_function("() => Boolean(document.fullscreenElement)")
    expect(fullscreen).to_have_attribute("aria-label", "Выйти из полноэкранного режима")
    mark("fullscreen-exit")
    fullscreen.click()
    page.wait_for_function("() => !document.fullscreenElement")
    mark("seek-end")
    seek.focus()
    seek.press("End")
    page.wait_for_function(
        "() => document.querySelector('.video-player').currentTime > 9.8"
    )
    # A native range with step=.1 may round End below duration (10.0 < 10.008).
    # Finish the remaining media first, then verify the real ended -> replay path.
    mark("natural-ended-after-seek")
    if not video.evaluate("v => v.ended"):
        center.click()
        page.wait_for_function("() => document.querySelector('.video-player').ended")
    expect(center).to_have_attribute("aria-label", "Смотреть заново")
    mark("restart-after-ended")
    center.click()
    page.wait_for_function(
        """() => {
          const v = document.querySelector('.video-player');
          return !v.paused && v.currentTime > 0.3 && v.currentTime < 3;
        }"""
    )
    toggle.click()

    mark("controls-auto-hide")
    center.click()
    page.mouse.move(0, 0)
    page.wait_for_function(
        """() => {
          const v = document.querySelector('.video-player');
          const controls = document.querySelector('[data-fox-controls]');
          return !v.paused && v.currentTime > 2.5 && controls.inert &&
            getComputedStyle(controls).opacity === '0';
        }"""
    )
    root.hover()
    expect(page.locator("[data-fox-controls]")).to_have_css("opacity", "1")
    require(
        page.locator("[data-fox-controls]").evaluate("c => !c.inert"),
        "Pointer movement did not restore interactive controls",
    )
    toggle.click()
    require(video.evaluate("v => v.paused"), "Restored pause button did not pause")

    report: dict[str, Any] = {
        "ui_play_pause_seek": "passed",
        "mouse_seek_seconds": mouse_seek,
        "keyboard_and_form_isolation": "passed",
        "mute_volume_fullscreen_restart": "passed",
        "controls_auto_hide": "hidden while playing; restored by pointer movement",
    }
    mark("native-fallback")
    fallback = browser.new_context(java_script_enabled=False)
    try:
        native = fallback.new_page()
        native.goto(base_url + watch_path)
        expect(native.locator(".video-player")).to_have_attribute("controls", "")
        expect(native.locator("[data-fox-controls]")).to_be_hidden()
        expect(native.locator("[data-fox-center]")).to_be_hidden()
        report["no_javascript"] = "native controls visible"
    finally:
        fallback.close()

    broken_init = browser.new_context()
    try:
        failed = broken_init.new_page()
        failed.route("**/static/player.js", lambda route: route.fulfill(body=""))
        failed.goto(base_url + watch_path)
        expect(failed.locator(".video-player")).to_have_attribute("controls", "")
        expect(failed.locator("[data-fox-controls]")).to_be_hidden()
        report["missing_component"] = "native controls visible"
    finally:
        broken_init.close()

    mark("touch-controls")
    mobile = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        locale="ru-RU",
    )
    try:
        touch = mobile.new_page()
        touch.on("pageerror", lambda error: errors.append(type(error).__name__))
        touch.goto(base_url + watch_path)
        ready(touch)
        no_overflow(touch)
        check_center_logo(touch)
        touch.screenshot(path=artifacts / "player-mobile.png", full_page=True)
        touch.locator("[data-fox-center]").tap()
        touch.wait_for_function(
            "() => document.querySelector('.video-player').currentTime > 0.5"
        )
        touch.locator("[data-fox-toggle]").tap()
        touch_seek = touch.locator("[data-fox-seek]")
        touch_seek.tap(
            position={"x": touch_seek.bounding_box()["width"] * 0.65, "y": 9}
        )
        touch.wait_for_function(
            """() => {
              const v = document.querySelector('.video-player');
              return v.paused && !v.seeking && v.currentTime > 5 && v.currentTime < 8;
            }"""
        )
        report["touch"] = "390x844; tap play/pause/seek passed"
        touch.goto(base_url + "/")
        mobile_card = touch.locator(f'.video-thumbnail[href="{watch_path}"]')
        expect(mobile_card).to_be_visible()
        expect(mobile_card.locator(".video-duration")).to_be_visible()
        expect(
            mobile_card.locator("xpath=..").locator(".video-card-info .channel-avatar")
        ).to_be_visible()
        expect(touch.locator(".thumbnail-preview")).to_have_count(0)
        no_overflow(touch)
        touch.screenshot(path=artifacts / "preview-mobile.png", full_page=True)
        report["touch_thumbnail"] = "390px card with channel avatar and duration"
    finally:
        mobile.close()

    mark("buffering-animation-and-errors")
    report["loading_and_failure"] = check_loading(
        browser, base_url, watch_path, artifacts, errors
    )
    mark("request-and-navigation-loaders")
    report["request_loaders"] = check_request_loaders(page, base_url)
    mark("hover-preview")
    report["preview"] = check_preview(page, base_url, watch_path, artifacts)
    page.goto(base_url + watch_path)
    ready(page)
    mark("passed")
    return report


def check_loading(
    browser: Browser,
    base_url: str,
    watch_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, str]:
    def mark(stage: str) -> None:
        (artifacts / "player-progress.json").write_text(
            json.dumps(
                {
                    "stage": stage,
                    "playback_state": playback_state(page),
                    "held_media_requests": [
                        {
                            "index": index,
                            "failed": bool(route.request.failure),
                            "aborted": "ERR_ABORTED" in (route.request.failure or ""),
                        }
                        for index, route in enumerate(held)
                    ],
                }
            )
            + "\n"
        )

    context = browser.new_context(viewport={"width": 1440, "height": 900})
    held: list[Route] = []
    released = False

    def hold_media(route: Route) -> None:
        if released:
            route.continue_()
        else:
            held.append(route)

    page = context.new_page()
    try:
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.route("**/media/videos/*/playback", hold_media)
        page.goto(base_url + watch_path, wait_until="domcontentloaded")
        mark("buffering-real-waiting")
        page.locator("[data-fox-center]").click()
        expect(page.locator("[data-fox-loading]")).to_be_visible()
        require(bool(held), "Buffering fixture did not intercept a real media request")
        page.screenshot(path=artifacts / "player-buffering.png", full_page=True)
        mark("buffering-loader-animation-and-reduced-motion")
        check_animation(page)
        mark("buffering-resume-real-playback")
        released = True
        for index, route in enumerate(tuple(held)):
            mark(f"buffering-release-media-{index}")
            route.continue_()
        page.unroute("**/media/videos/*/playback")
        mark("buffering-await-resumed-playback")
        page.wait_for_function(
            "() => document.querySelector('.video-player').currentTime > 0.4"
        )
        expect(page.locator("[data-fox-loading]")).to_be_hidden()
        page.locator("[data-fox-toggle]").click()
        mark("controlled-play-rejection")
        page.locator(".video-player").evaluate(
            """v => {
              v.play = () => Promise.reject(
                new DOMException('blocked', 'NotAllowedError'));
            }"""
        )
        page.locator("[data-fox-center]").click()
        expect(page.locator("[data-fox-message]")).to_contain_text("Не удалось начать")
        expect(page.locator("[data-fox-loading]")).to_be_hidden()
        mark("controlled-media-404")
        page.route(
            "**/media/videos/*/playback",
            lambda route: route.fulfill(status=404, body="Media unavailable"),
        )
        page.goto(base_url + watch_path)
        expect(page.locator("[data-fox-message]")).to_contain_text(
            "Не удалось воспроизвести"
        )
        expect(page.locator("[data-fox-loading]")).to_be_hidden()
        return {
            "buffering": "real media request held; playing resumed after release",
            "play_rejection": "controlled rejected play() handled without pageerror",
            "media_failure": "HTTP 404 shows error and stops loader",
        }
    except BaseException as error:
        record_failure(
            artifacts / "player-progress.json", error, page, "playback_state"
        )
        raise
    finally:
        context.close()


def check_preview(
    page: Page, base_url: str, watch_path: str, artifacts: Path
) -> dict[str, Any]:
    requests: list[str] = []

    def dwell_without_autoplay() -> None:
        # Stay hovered beyond the 350ms activation threshold in blocked modes.
        started = page.evaluate("() => performance.now()")
        page.wait_for_function(
            "started => performance.now() - started >= 600", arg=started
        )

    def record(request: Any) -> None:
        if "/media/videos/" in request.url and request.url.endswith("/playback"):
            requests.append("playback")

    page.on("request", record)
    try:
        page.goto(base_url + "/")
        card = page.locator(f'.video-thumbnail[href="{watch_path}"]')
        expect(card).to_be_visible()
        article = card.locator("xpath=..")
        avatar = article.locator(".video-card-info .channel-avatar")
        expect(avatar).to_be_visible()
        expect(avatar).to_have_attribute(
            "href", article.locator(".video-channel-name a").get_attribute("href")
        )
        avatar_box = avatar.bounding_box()
        title_box = article.locator("h2").bounding_box()
        require(
            avatar_box["x"] + avatar_box["width"] <= title_box["x"] + 1,
            "Channel avatar overlaps the title instead of sitting to its left",
        )
        require(not requests, "Grid fetched MP4 before hover")
        expect(page.locator(".thumbnail-preview")).to_have_count(0)
        expect(card.locator(".thumbnail-preview-sound")).to_have_count(0)
        expect(card.locator(".thumbnail-preview-progress")).to_have_count(0)
        card.hover()
        preview = card.locator(".thumbnail-preview")
        expect(preview).to_be_visible()
        page.wait_for_function(
            """() => {
              const v = document.querySelector('.thumbnail-preview');
              return v && v.muted && !v.paused && v.currentTime > 0.5;
            }"""
        )
        require(bool(requests), "Hover did not request protected MP4")
        sound = card.locator(".thumbnail-preview-sound")
        expect(sound).to_be_visible()
        expect(sound).to_have_css("opacity", "1")
        expect(sound.locator("svg")).to_have_count(1)
        sound_box = sound.bounding_box()
        require(
            0 < sound_box["width"] <= 44 and 0 < sound_box["height"] <= 44,
            "Muted preview indicator is not compact",
        )
        duration = card.locator(".video-duration")
        expect(duration).to_be_visible()
        duration_box = duration.bounding_box()
        card_box = card.bounding_box()
        require(
            0
            <= card_box["x"]
            + card_box["width"]
            - duration_box["x"]
            - duration_box["width"]
            <= 24
            and 0
            <= card_box["y"]
            + card_box["height"]
            - duration_box["y"]
            - duration_box["height"]
            <= 24,
            "Video duration is not visible at the thumbnail's bottom right",
        )
        require(
            card.evaluate(
                "c => !getComputedStyle(c, '::after').content.includes('первые 20%')"
            ),
            "Preview still displays the former text badge",
        )
        progress = card.locator(".thumbnail-preview-progress")
        expect(progress).to_be_visible()
        expect(progress).to_have_css("opacity", "1")
        expect(progress.locator(":scope > span")).to_have_count(1)
        require(
            0 < progress.bounding_box()["height"] <= 4,
            "Preview progress track is not a thin line",
        )
        preview.evaluate(
            """v => {
              const observation = window.previewObservation = {
                loops: 0, maximum: 0, progressMaximum: 0,
                progressAdvances: 0, progressResets: 0, progressError: 0
              };
              let previous = v.currentTime;
              v.addEventListener('timeupdate', () => {
                if (v.currentTime + 0.5 < previous) {
                  observation.loops += 1;
                }
                observation.maximum = Math.max(observation.maximum, v.currentTime);
                previous = v.currentTime;
              });
              const track = v.parentElement.querySelector(
                '.thumbnail-preview-progress');
              const fill = track.querySelector('span');
              let previousProgress = 0;
              let sawHighProgress = false;
              const sample = () => {
                if (!v.isConnected) return;
                const fraction = fill.getBoundingClientRect().width /
                  track.getBoundingClientRect().width;
                observation.progressMaximum = Math.max(
                  observation.progressMaximum, fraction);
                if (fraction > previousProgress + 0.003) observation.progressAdvances++;
                if (fraction > 0.75) sawHighProgress = true;
                if (sawHighProgress && fraction < 0.25) {
                  observation.progressResets++;
                  sawHighProgress = false;
                }
                const limit = v.duration * 0.2;
                if (!v.seeking && v.currentTime > 0.3 && v.currentTime < limit - 0.3) {
                  observation.progressError = Math.max(observation.progressError,
                    Math.abs(fraction - v.currentTime / limit));
                }
                previousProgress = fraction;
                requestAnimationFrame(sample);
              };
              requestAnimationFrame(sample);
            }"""
        )
        page.wait_for_function(
            "() => window.previewObservation.loops >= 2 && "
            "window.previewObservation.progressResets >= 2"
        )
        observation = page.evaluate("() => window.previewObservation")
        require(
            1.0 < observation["maximum"] <= 2.4,
            "Preview escaped the first 20% of the ten-second video",
        )
        require(
            0.75 < observation["progressMaximum"] <= 1.01
            and observation["progressAdvances"] >= 10
            and observation["progressError"] < 0.2,
            "Rendered preview progress does not follow the first 20% playback",
        )
        expect(page.locator(".thumbnail-preview")).to_have_count(1)
        no_overflow(page)
        page.screenshot(path=artifacts / "preview-desktop.png", full_page=True)
        page.mouse.move(0, 0)
        expect(page.locator(".thumbnail-preview")).to_have_count(0)
        expect(page.locator(".thumbnail-preview-sound")).to_have_count(0)
        expect(page.locator(".thumbnail-preview-progress")).to_have_count(0)
        page.emulate_media(reduced_motion="reduce")
        count = len(requests)
        card.hover()
        expect(card).to_have_css("position", "relative")
        dwell_without_autoplay()
        expect(page.locator(".thumbnail-preview")).to_have_count(0)
        require(len(requests) == count, "Reduced motion fetched a hover video")
        page.emulate_media(reduced_motion="no-preference")
        page.mouse.move(0, 0)
        page.evaluate(
            """() => Object.defineProperty(navigator.connection, 'saveData', {
              configurable: true, value: true
            })"""
        )
        card.hover()
        dwell_without_autoplay()
        expect(page.locator(".thumbnail-preview")).to_have_count(0)
        require(len(requests) == count, "Data saver fetched a hover video")
        page.evaluate("() => delete navigator.connection.saveData")
        return {
            "lazy_request": "no MP4 before hover; protected MP4 after hover",
            "muted": True,
            "loops": observation["loops"],
            "maximum_seconds": observation["maximum"],
            "compact_mute_icon_duration_and_avatar": "passed",
            "progress_advances": observation["progressAdvances"],
            "progress_resets": observation["progressResets"],
            "progress_maximum_fraction": observation["progressMaximum"],
            "progress_maximum_error": observation["progressError"],
            "leave_unloads": "passed",
            "reduced_motion_and_save_data": "no autoplay",
        }
    finally:
        page.remove_listener("request", record)
        page.emulate_media(reduced_motion="no-preference")
