"""Error pages and recovery against real server responses in isolated Chrome."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from miniplayer import remember_video, same_video
from player import no_overflow, ready, require
from playwright.sync_api import Browser, Page, expect


def assert_error(page: Page, status: int, title: str) -> None:
    expect(
        page.locator(f'#main-content [data-error-status="{status}"]')
    ).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_text(title)
    expect(page.locator(".site-header")).to_be_visible()
    expect(page.get_by_role("link", name="На главную", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Найти видео", exact=True)).to_be_visible()
    require(
        page.locator("#main-content").inner_text().find('{"detail"') == -1,
        "Error page displays technical JSON",
    )
    no_overflow(page)


def internal_link(page: Page, path: str) -> None:
    # A stale server link can become unavailable after the document was loaded.
    page.locator(".brand").evaluate("(link, path) => { link.href = path; }", path)
    page.locator(".brand").click()


def check_error_pages(
    browser: Browser, owner: Page, base_url: str, watch_path: str, artifacts: Path
) -> dict[str, Any]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
        storage_state=owner.context.storage_state(),
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    # Browser console reports expected HTTP4xx as errors; JS exceptions are
    # tracked separately so expected status codes cannot mask script failures.
    report: dict[str, Any] = {"status": "failed"}
    missing = str(uuid4())
    cases = [
        ("/unknown-page", 404, "Страница недоступна"),
        ("/nested/missing/page", 404, "Страница недоступна"),
        (f"/watch/{missing}", 404, "Страница недоступна"),
        (f"/channels/{missing}", 404, "Страница недоступна"),
        (f"/playlists/{missing}", 404, "Страница недоступна"),
        ("/@missing-" + missing, 404, "Страница недоступна"),
        ("/watch/not-a-uuid", 422, "Проверьте адрес"),
        ("/channels/not-a-uuid", 422, "Проверьте адрес"),
        ("/playlists/not-a-uuid", 422, "Проверьте адрес"),
        ("/search?page=0", 422, "Проверьте адрес"),
        ("/?page=not-a-number", 422, "Проверьте адрес"),
        ("/search?q=" + "a" * 201, 422, "Проверьте адрес"),
        ("/admin", 403, "Доступ ограничен"),
        ("/moderation", 403, "Доступ ограничен"),
    ]
    try:
        report["phase"] = "direct_navigation"
        for path, status, title in cases:
            response = page.goto(base_url + path)
            require(response is not None and response.status == status, path)
            require(
                response.headers.get("content-type", "").startswith("text/html"),
                "Browser error did not return HTML",
            )
            require(
                "no-store" in response.headers.get("cache-control", ""),
                "Error page permits caching",
            )
            assert_error(page, status, title)
        report["direct_navigation"] = {"cases": len(cases), "status": "passed"}

        report["phase"] = "api_contract"
        for path, status in [
            ("/api/v1/missing", 404),
            (f"/api/v1/videos/{missing}", 404),
            ("/api/v1/videos/not-a-uuid", 422),
            ("/api/v1/videos?page=0", 422),
            (f"/media/videos/{missing}/playback", 404),
            (f"/media/videos/{missing}/poster", 404),
            ("/static/missing.js", 404),
            ("/health/missing", 404),
        ]:
            response = context.request.get(
                base_url + path, headers={"Accept": "text/html"}
            )
            require(response.status == status, "Service route changed error status")
            require(
                response.headers.get("content-type", "").startswith("application/json")
                and "detail" in response.json(),
                "HTML Accept changed the machine error contract",
            )
            response.dispose()
        response = context.request.post(base_url + watch_path)
        require(
            response.status == 405 and response.headers.get("allow") == "GET",
            "Error handler lost HTTP405 or Allow",
        )
        response.dispose()
        report["api_contract"] = (
            "JSON for API/media/static/health, including HTML Accept"
        )

        report["phase"] = "recovery_and_mobile"
        for width, height in [(1440, 900), (390, 844), (320, 740)]:
            page.set_viewport_size({"width": width, "height": height})
            page.goto(base_url + "/unknown-page")
            assert_error(page, 404, "Страница недоступна")
            page.screenshot(path=artifacts / f"error-404-{width}.png", full_page=True)
            page.get_by_role("link", name="На главную", exact=True).click()
            page.wait_for_url(base_url + "/")
            expect(page.locator("#page-title")).to_have_text("Главная")
            page.goto(base_url + "/watch/not-a-uuid")
            assert_error(page, 422, "Проверьте адрес")
            page.get_by_role("link", name="Найти видео", exact=True).click()
            page.wait_for_url(base_url + "/search")
            expect(page.get_by_role("heading", level=1)).to_have_text("Поиск видео")
            page.go_back()
            assert_error(page, 422, "Проверьте адрес")
        report["recovery"] = (
            "home/search/back; desktop1440/mobile390/320 without overflow"
        )

        report["phase"] = "miniplayer"
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(base_url + watch_path)
        ready(page)
        remember_video(page)
        player = page.locator("[data-fox-player] video")
        player.evaluate("async video => { video.muted = true; await video.play(); }")
        page.wait_for_function("() => window.__miniQA.video.currentTime > 0.3")
        with page.expect_response(base_url + f"/watch/{missing}") as unavailable:
            internal_link(page, f"/watch/{missing}")
        require(unavailable.value.status == 404, "Stale watch link lost404")
        page.wait_for_url(base_url + f"/watch/{missing}")
        assert_error(page, 404, "Страница недоступна")
        same_video(page)
        expect(page.locator("[data-miniplayer]")).to_be_visible()
        before = page.evaluate("() => window.__miniQA.video.currentTime")
        page.wait_for_function(
            "time => window.__miniQA.video.currentTime > time + .2",
            arg=before,
        )
        player.evaluate("video => video.pause()")
        page.screenshot(path=artifacts / "error-404-miniplayer.png", full_page=True)
        page.locator("[data-mini-restore]").click()
        page.wait_for_url(base_url + watch_path)
        same_video(page)
        expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
        with page.expect_response(base_url + "/search?page=0") as invalid:
            internal_link(page, "/search?page=0")
        require(invalid.value.status == 422, "Invalid search lost422")
        page.wait_for_url(base_url + "/search?page=0")
        assert_error(page, 422, "Проверьте адрес")
        same_video(page)
        expect(page.locator("[data-miniplayer]")).to_be_visible()
        page.go_back()
        page.wait_for_url(base_url + watch_path)
        # History changes its URL before the asynchronous server-page fetch and
        # player attachment finish. Autoplay below starts from the main player.
        expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
        page.wait_for_function("() => window.FoxMiniPlayer.getPlayer()?.mini === false")
        same_video(page)

        report["phase"] = "automatic_navigation"
        report["automatic_before"] = page.evaluate(
            """() => {
              const player = window.FoxMiniPlayer.getPlayer();
              return {mini: player?.mini, originalVideo:
                player?.video === window.__miniQA.video};
            }"""
        )
        with page.expect_response(base_url + f"/watch/{missing}") as automatic:
            accepted = page.evaluate(
                """async id => {
                  const player = window.FoxMiniPlayer.getPlayer();
                  return window.FoxMiniPlayer.playNext({id, title: 'Unavailable',
                    playback_url: `/media/videos/${id}/playback`,
                    poster_url: `/media/videos/${id}/poster`}, player.root, () => true);
                }""",
                missing,
            )
        report["automatic_result"] = accepted
        report["automatic_after"] = page.evaluate(
            """() => {
              const player = window.FoxMiniPlayer.getPlayer();
              return {mini: player?.mini, originalVideo:
                player?.video === window.__miniQA.video};
            }"""
        )
        require(automatic.value.status == 404, "Autoplay did not receive the real404")
        require(accepted is False, "Autoplay accepted a missing-video error page")
        require(
            page.url == base_url + watch_path, "Autoplay navigated to an error page"
        )
        same_video(page)
        expect(page.locator("#main-content [data-fox-player]")).to_be_visible()
        report["miniplayer"] = (
            "same document/video/media on404/422/back/restore; currentTime advances"
        )
        report["automatic_navigation"] = "real404 rejected; current player preserved"
        require(not errors, "Error pages produced JavaScript exceptions")
        report["javascript_errors"] = errors
        report["status"] = "passed"
        return report
    finally:
        if report["status"] != "passed":
            page.screenshot(path=artifacts / "error-page-failure.png", full_page=True)
        (artifacts / "errors.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        context.close()
