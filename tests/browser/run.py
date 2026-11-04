"""Real Chrome check; run only via scripts/test-browser.sh on isolated data."""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from lifecycle import check_lifecycle
from player import check_player
from playwright.sync_api import Page, expect, sync_playwright
from privacy import check_privacy
from staff import check_moderation
from swagger import check_swagger_upload


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def create_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(path),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    require(result.returncode == 0, "Actual FFmpeg could not generate the test video")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    require(probe.returncode == 0, "Actual video fixture failed ffprobe")
    return {
        "size_bytes": path.stat().st_size,
        "streams": [
            {key: stream[key] for key in ("codec_type", "codec_name")}
            for stream in json.loads(probe.stdout)["streams"]
        ],
    }


def assert_no_overflow(page: Page) -> None:
    require(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        ),
        "Page has horizontal overflow at the tested viewport",
    )


def check_social(page: Page, base_url: str, watch_path: str) -> dict[str, Any]:
    like = page.locator("[data-like]")
    expect(like).to_be_enabled()
    like.click()
    expect(like).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-likes-count]")).to_have_text("1")
    comment = "Проверка сохранения: <script>window.commentExecuted=true</script>"
    page.get_by_label("Ваш комментарий", exact=True).fill(comment)
    page.get_by_role("button", name="Оставить комментарий", exact=True).click()
    expect(page.locator("[data-comments-list]")).to_contain_text(comment)
    page.reload()
    expect(page.locator("[data-like]")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-comments-list]")).to_contain_text(comment)
    require(
        page.evaluate("() => !window.commentExecuted"), "Comment executed JavaScript"
    )

    page.goto(f"{base_url}/playlists")
    page.locator("details summary").click()
    page.get_by_label("Название", exact=True).fill("Реальная подборка Chrome")
    page.get_by_label("Кто может видеть плейлист", exact=True).select_option("public")
    page.get_by_role("button", name="Создать", exact=True).click()
    page.wait_for_url(f"{base_url}/playlists/*")
    playlist_path = urlsplit(page.url).path
    page.goto(base_url + watch_path)
    page.locator("[data-save-toggle]").click()
    page.locator("[data-save-playlist] select").select_option(
        playlist_path.rsplit("/", 1)[1]
    )
    page.locator("[data-save-playlist] button[type=submit]").click()
    expect(page.locator("[data-save-result]")).to_be_visible()
    page.goto(base_url + playlist_path)
    expect(page.locator(f'a[href="{watch_path}"]').first).to_be_visible()
    assert_no_overflow(page)
    page.goto(base_url + watch_path)
    expect(page.locator("[data-like]")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-likes-count]")).to_have_text("1")
    expect(page.locator("[data-comments-list]")).to_contain_text(comment)
    expect(page.locator("[data-comments-more]")).to_be_hidden()
    expect(page.locator("[data-grants-empty]")).to_have_text(
        "Разрешения пока не выданы."
    )
    return {
        "like": "persisted",
        "comment": "persisted and escaped",
        "playlist": playlist_path,
    }


def main() -> None:
    base_url = os.environ["FOXTUBE_TEST_BASE_URL"].rstrip("/")
    require(
        urlsplit(base_url).hostname == "nginx",
        "Browser checks must target the disposable Compose test project",
    )
    artifacts = Path(os.environ.get("FOXTUBE_BROWSER_ARTIFACTS", "/artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"status": "failed", "base_url": base_url}
    errors: list[str] = []
    playback_responses: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="foxtube-e2e-") as temporary:
        source = Path(temporary) / "real-video.mp4"
        report["input"] = create_video(source)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            report["browser"] = browser.version
            context = browser.new_context(
                viewport={"width": 1440, "height": 900}, locale="ru-RU"
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            page.on(
                "console",
                lambda message: (
                    errors.append("console:error") if message.type == "error" else None
                ),
            )

            def record_playback(response: Any) -> None:
                if "/media/videos/" in response.url and response.url.endswith(
                    "/playback"
                ):
                    playback_responses.append(
                        {
                            "status": response.status,
                            "range": response.request.headers.get("range"),
                        }
                    )

            page.on("response", record_playback)
            try:
                page.goto(f"{base_url}/register")
                page.get_by_label("Имя", exact=True).fill("Автор проверки видео")
                owner_email = f"video-e2e-{uuid4().hex}@example.com"
                owner_password = secrets.token_urlsafe(32)
                page.get_by_label("Email", exact=True).fill(owner_email)
                page.get_by_label("Пароль", exact=True).fill(owner_password)
                page.get_by_role("button", name="Создать аккаунт", exact=True).click()
                page.wait_for_url(f"{base_url}/account")
                report["registration"] = "passed"

                page.goto(f"{base_url}/upload")
                title = f"Настоящее видео Chrome {uuid4().hex[:10]}"
                page.locator('input[type="file"]').set_input_files(source)
                page.locator('[name="title"]').fill(title)
                page.locator('[name="description"]').fill(
                    "Загрузка, FFmpeg, H.264, воспроизведение и перемотка."
                )
                page.locator('[name="visibility"]').select_option("public")
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and urlsplit(response.url).path == "/api/v1/videos"
                    ),
                    timeout=30000,
                ) as created:
                    page.locator(
                        'form:has(input[type="file"]) button[type="submit"]'
                    ).click()
                response = created.value
                require(response.status == 201, "UI metadata creation failed")
                expect(page.locator("[data-upload-loader]")).to_be_visible()
                created_video = response.json()
                video_id = created_video["id"]
                watch_path = f"/watch/{video_id}"
                report["video_id"] = video_id
                page.wait_for_function(
                    """path => location.pathname === path ||
                    Array.from(document.querySelectorAll('a')).some(a =>
                      a.getAttribute('href') === path && a.getClientRects().length > 0)
                    """,
                    arg=watch_path,
                    timeout=120000,
                )
                if urlsplit(page.url).path != watch_path:
                    expect(page.locator("[data-upload-loader]")).to_be_hidden()
                    page.locator(f'a[href="{watch_path}"]').first.click()
                page.wait_for_url(f"{base_url}{watch_path}")
                player = page.locator("video")
                expect(player).to_be_visible()
                page.wait_for_function(
                    """() => {
                      const video = document.querySelector('video');
                      return video && video.readyState >= 2 && !video.error;
                    }""",
                    timeout=30000,
                )
                supported = player.evaluate(
                    "video => video.canPlayType("
                    "'video/mp4; codecs=\"avc1.42E01E, mp4a.40.2\"')"
                )
                require(
                    bool(supported), "Chrome does not support the required H.264/AAC"
                )
                report["h264_support"] = supported
                report["duration"] = player.evaluate("video => video.duration")
                require(9.5 <= report["duration"] <= 10.5, "Wrong playback duration")
                player.evaluate("video => video.play()")
                page.wait_for_function(
                    "() => document.querySelector('video').currentTime > 1.0",
                    timeout=20000,
                )
                report["playing_time"] = player.evaluate("video => video.currentTime")
                player.evaluate("video => { video.pause(); video.currentTime = 7; }")
                page.wait_for_function(
                    """() => {
                      const video = document.querySelector('video');
                      return !video.seeking && video.readyState >= 2 &&
                        Math.abs(video.currentTime - 7) < 0.6 && !video.error;
                    }""",
                    timeout=20000,
                )
                report["seek_time"] = player.evaluate("video => video.currentTime")
                player.evaluate("video => video.play()")
                page.wait_for_function(
                    "() => document.querySelector('video').currentTime > 8.0",
                    timeout=20000,
                )
                report["playing_after_seek"] = player.evaluate(
                    "video => video.currentTime"
                )
                player.evaluate("video => video.pause()")
                report["styled_player"] = check_player(
                    browser, page, base_url, watch_path, artifacts, errors
                )
                report["social"] = check_social(page, base_url, watch_path)
                player = page.locator("video")
                assert_no_overflow(page)
                page.screenshot(path=artifacts / "watch-desktop.png", full_page=True)

                viewer_context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, locale="ru-RU"
                )
                try:
                    viewer = viewer_context.new_page()
                    viewer.on(
                        "pageerror", lambda error: errors.append(type(error).__name__)
                    )
                    viewer.goto(f"{base_url}/register")
                    viewer.get_by_label("Имя", exact=True).fill("Зритель проверки")
                    viewer_email = f"viewer-{uuid4().hex}@example.com"
                    viewer.get_by_label("Email", exact=True).fill(viewer_email)
                    viewer.get_by_label("Пароль", exact=True).fill(
                        "Disposable-viewer-passphrase-42!"
                    )
                    viewer.get_by_role(
                        "button", name="Создать аккаунт", exact=True
                    ).click()
                    viewer.wait_for_url(f"{base_url}/account")
                    viewer.goto(f"{base_url}/channels/{created_video['channel_id']}")
                    expect(
                        viewer.locator(".primary-nav .nav-item.is-active")
                    ).to_have_count(0)
                    viewer.get_by_role("link", name="Мой канал", exact=True).click()
                    expect(
                        viewer.locator(".primary-nav .nav-item.is-active")
                    ).to_have_text("Мой канал")
                    expect(viewer.get_by_role("heading", level=1)).to_have_text(
                        "Зритель проверки"
                    )
                    viewer.goto(f"{base_url}/channels/{created_video['channel_id']}")
                    subscription = viewer.locator("[data-channel-subscription] button")
                    expect(subscription).to_have_text("Подписаться")
                    subscription.click()
                    expect(
                        viewer.locator("[data-channel-subscription] button")
                    ).to_have_attribute("aria-pressed", "true")
                    viewer.reload()
                    expect(
                        viewer.locator("[data-channel-subscription] button")
                    ).to_have_attribute("aria-pressed", "true")
                    expect(
                        viewer.locator(
                            f'a[href="{report["social"]["playlist"]}"]'
                        ).first
                    ).to_be_visible()
                    assert_no_overflow(viewer)
                    viewer.screenshot(
                        path=artifacts / "channel-desktop.png", full_page=True
                    )
                    viewer.goto(f"{base_url}/subscriptions")
                    expect(
                        viewer.locator(f'a[href="{watch_path}"]').first
                    ).to_be_visible()
                    viewer.goto(base_url + watch_path)
                    viewer.locator("details.video-report summary").click()
                    viewer.get_by_label("Причина жалобы").fill(
                        "Реальная жалоба из браузерной проверки."
                    )
                    viewer.get_by_role(
                        "button", name="Отправить жалобу", exact=True
                    ).click()
                    expect(
                        viewer.locator("[data-video-report] [data-moderation-message]")
                    ).to_contain_text("Жалоба")
                    report["subscriptions_and_report"] = "persisted through UI"
                    report["staff"] = check_moderation(
                        browser, base_url, video_id, viewer, viewer_email, errors
                    )
                    viewer.goto(f"{base_url}/channels/{created_video['channel_id']}")
                    subscription = viewer.locator("[data-channel-subscription] button")
                    expect(subscription).to_have_attribute("aria-pressed", "true")
                    subscription.click()
                    expect(subscription).to_have_attribute("aria-pressed", "false")
                    viewer.reload()
                    expect(
                        viewer.locator("[data-channel-subscription] button")
                    ).to_have_attribute("aria-pressed", "false")
                    viewer.goto(f"{base_url}/subscriptions")
                    expect(viewer.locator(f'a[href="{watch_path}"]')).to_have_count(0)
                    report["unsubscribe"] = "persisted; removed from subscription feed"
                finally:
                    viewer_context.close()

                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_function(
                    """() => {
                      const main = document.querySelector('main');
                      const sidebar = document.querySelector('#site-sidebar');
                      return document.body.dataset.sidebarOpen === 'false' &&
                        main.getBoundingClientRect().left === 0 &&
                        sidebar.getBoundingClientRect().right <= 1;
                    }"""
                )
                mobile_player = player.bounding_box()
                require(
                    mobile_player is not None
                    and abs(mobile_player["x"]) <= 1
                    and mobile_player["width"] >= 388,
                    "Mobile player must use the available viewport width",
                )
                report["mobile_player_width"] = mobile_player["width"]
                assert_no_overflow(page)
                page.screenshot(path=artifacts / "watch-mobile.png", full_page=True)

                page.goto(f"{base_url}/")
                expect(page.locator(f'a[href="{watch_path}"]').first).to_be_visible()
                assert_no_overflow(page)
                page.screenshot(path=artifacts / "home-mobile.png", full_page=True)
                page.goto(f"{base_url}/search?q={title}")
                expect(page.locator(f'a[href="{watch_path}"]').first).to_be_visible()
                report["privacy"] = check_privacy(
                    browser,
                    page,
                    base_url,
                    source,
                    report["social"]["playlist"],
                    artifacts,
                    errors,
                )
                report["swagger"] = check_swagger_upload(
                    page, base_url, source, artifacts
                )
                report["lifecycle"] = check_lifecycle(
                    browser,
                    page,
                    base_url,
                    video_id,
                    owner_email,
                    owner_password,
                    report["social"]["playlist"],
                    artifacts,
                    errors,
                )
                require(not errors, "Browser reported a JavaScript exception")
                require(
                    any(item["status"] in {200, 206} for item in playback_responses),
                    "Player did not request its protected media through Nginx",
                )
                report["playback_responses"] = playback_responses
                report["javascript_errors"] = errors
                report["viewports"] = ["1440x900", "390x844"]
                report["status"] = "passed"
            except BaseException as error:
                report["status"] = "failed"
                report["failure_type"] = type(error).__name__
                # No request/response dumps or browser storage: they contain secrets.
                try:
                    page.screenshot(
                        path=artifacts / "failure.png",
                        full_page=True,
                        mask=[
                            page.locator(
                                'input[type="password"], '
                                ".curl-command, .request-snippets"
                            )
                        ],
                    )
                except Exception:
                    report["failure_screenshot"] = "unavailable"
                raise AssertionError(
                    "Chrome E2E failed; see safe report.json"
                ) from None
            finally:
                close_failed = False
                for resource in (context, browser):
                    try:
                        resource.close()
                    except Exception as error:
                        close_failed = True
                        report["status"] = "failed"
                        report["failure_type"] = type(error).__name__
                (artifacts / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
                if close_failed:
                    raise RuntimeError(
                        "Chrome cleanup failed; see safe report.json"
                    ) from None
    print(
        "Chrome E2E passed: registration, UI upload, FFmpeg, H.264, "
        "advancing currentTime, seek, likes/comments/playlists, "
        "subscription/report, role change, moderation, audit, "
        "last admin protection, search, private sharing/revocation, "
        "logout/login, edit/delete, social undo, desktop/mobile."
    )


if __name__ == "__main__":
    main()
