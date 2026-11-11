"""Chrome acceptance against an already running local/LAN FoxTube deployment.

Run inside the existing Dockerfile.browser image. Host networking is needed for
a loopback target; a LAN target uses normal container egress. Mount the
repository's tests read-only at /tests and a writable artifacts directory at
/artifacts. Set PYTHONPATH=/tests/browser, FOXTUBE_TEST_BASE_URL (default
http://localhost:8080; loopback/RFC1918 IPv4 only) and optionally
FOXTUBE_BROWSER_ARTIFACTS (default /artifacts/ubuntu); execute
python /tests/deployment/browser.py. The runtime's public origin must match the
chosen origin; this runner never changes server configuration or bypasses CSRF.

No Compose, database access, staff fixture or test environment is required.
Creates four ordinary accounts and two synthetic videos through the UI; their
passwords stay in memory and are not saved. Test data remains in the deployment.
"""

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from network import validate_origin
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright
from privacy import capture, check_lists, check_media, play_and_seek
from run import assert_no_overflow, check_social, create_video, require


def register(page: Page, base_url: str, name: str) -> str:
    email = f"ubuntu-qa-{uuid4().hex}@example.com"
    page.goto(f"{base_url}/register")
    page.get_by_label("Имя", exact=True).fill(name)
    page.get_by_label("Email", exact=True).fill(email)
    page.get_by_label("Пароль", exact=True).fill(secrets.token_urlsafe(32))
    page.get_by_role("button", name="Создать аккаунт", exact=True).click()
    page.wait_for_url(f"{base_url}/account")
    return email


def mobile(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        """() => {
          const sidebar = document.querySelector('#site-sidebar');
          return document.body.dataset.sidebarOpen === 'false' &&
            sidebar.getBoundingClientRect().right <= 1;
        }"""
    )
    assert_no_overflow(page)


def upload(page: Page, base_url: str, source: Path, visibility: str) -> dict[str, Any]:
    page.goto(f"{base_url}/upload")
    page.locator('input[type="file"]').set_input_files(source)
    page.locator('[name="title"]').fill(
        f"Ubuntu Chrome {visibility} {uuid4().hex[:10]}"
    )
    page.locator('[name="description"]').fill(
        "Проверка установленного FoxTube: настоящий FFmpeg и Chrome H.264/AAC."
    )
    page.locator('[name="visibility"]').select_option(visibility)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/api/v1/videos"
        ),
        timeout=30000,
    ) as created:
        page.locator('form:has(input[type="file"]) button[type="submit"]').click()
    require(created.value.status == 201, "Deployment UI metadata creation failed")
    video = created.value.json()
    watch_path = f"/watch/{video['id']}"
    link = page.locator(f'[data-upload-watch][href="{watch_path}"]')
    expect(link).to_be_visible(timeout=180000)
    link.click()
    page.wait_for_url(base_url + watch_path)
    expect(page.locator("[data-watch-video]")).to_have_attribute(
        "data-video-status", "READY"
    )
    return video


def check_private(
    browser: Browser,
    owner: Page,
    base_url: str,
    source: Path,
    playlist_path: str,
    artifacts: Path,
    errors: list[str],
    progress: dict[str, Any],
) -> dict[str, Any]:
    contexts: list[BrowserContext] = []

    def new_page() -> Page:
        context = browser.new_context(
            locale="ru-RU", viewport={"width": 1440, "height": 900}
        )
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on(
            "console",
            lambda message: (
                errors.append("console:error") if message.type == "error" else None
            ),
        )
        return page

    try:
        progress["phase"] = "ordinary_user_registration"
        first, second, outsider = [new_page() for _ in range(3)]
        first_email = register(first, base_url, "Первый приглашённый Ubuntu")
        second_email = register(second, base_url, "Второй приглашённый Ubuntu")
        register(outsider, base_url, "Посторонний Ubuntu")
        guest = browser.new_context(locale="ru-RU")
        contexts.append(guest)
        owner.set_viewport_size({"width": 1440, "height": 900})
        progress["phase"] = "private_ui_upload"
        video = upload(owner, base_url, source, "private")
        video_id = video["id"]
        progress["video_id"] = video_id
        watch_path = f"/watch/{video_id}"
        expect(owner.locator(".watch-heading .privacy-badge")).to_have_text("Приватное")
        expect(owner.locator("[data-grants-empty]")).to_have_text(
            "Разрешения пока не выданы."
        )
        for role, context in (
            ("first_recipient", first.context),
            ("second_recipient", second.context),
            ("outsider", outsider.context),
            ("anonymous", guest),
        ):
            progress["phase"] = f"before_grants:{role}"
            check_media(context, base_url, video_id, allowed=False)
        progress["phase"] = "grant_two_recipients_via_ui"
        for email in (first_email, second_email):
            owner.get_by_label("Email пользователя", exact=True).fill(email)
            owner.get_by_role("button", name="Разрешить просмотр", exact=True).click()
            expect(owner.locator("[data-grants-list]")).to_contain_text(email)
        owner.reload()
        expect(owner.locator("[data-grants-list] li")).to_have_count(2)

        owner.locator("[data-save-toggle]").click()
        owner.locator("[data-save-playlist] select").select_option(
            playlist_path.rsplit("/", 1)[1]
        )
        owner.locator("[data-save-playlist] button[type=submit]").click()
        expect(owner.locator("[data-save-result]")).to_be_visible()
        for role, context, allowed in (
            ("owner", owner.context, True),
            ("first_recipient", first.context, True),
            ("second_recipient", second.context, True),
            ("outsider", outsider.context, False),
            ("anonymous", guest, False),
        ):
            progress["phase"] = f"shared_media:{role}"
            check_media(context, base_url, video_id, allowed=allowed)
            progress["phase"] = f"shared_lists:{role}"
            check_lists(context, base_url, video, playlist_path, allowed=allowed)
        progress["phase"] = "owner_desktop_mobile"
        assert_no_overflow(owner)
        capture(owner, artifacts / "private-owner-desktop.png")
        mobile(owner)
        capture(owner, artifacts / "private-owner-mobile.png")
        progress["phase"] = "first_recipient_playback_desktop_mobile"
        first.goto(base_url + watch_path)
        expect(first.locator("[data-video-owner]")).to_have_count(0)
        playback = play_and_seek(first)
        assert_no_overflow(first)
        capture(first, artifacts / "private-recipient-desktop.png")
        mobile(first)
        capture(first, artifacts / "private-recipient-mobile.png")

        progress["phase"] = "selective_revoke_via_ui"
        owner.get_by_role("button", name=f"Отозвать доступ: {first_email}").click()
        expect(owner.locator("[data-grants-list] li")).to_have_count(1)
        expect(owner.locator("[data-grants-list]")).not_to_contain_text(first_email)
        expect(owner.locator("[data-grants-list]")).to_contain_text(second_email)
        # Check new requests; bytes already received by a player cannot be recalled.
        for role, context, allowed in (
            ("revoked_recipient", first.context, False),
            ("remaining_recipient", second.context, True),
        ):
            progress["phase"] = f"after_revoke_media:{role}"
            check_media(context, base_url, video_id, allowed=allowed)
            progress["phase"] = f"after_revoke_lists:{role}"
            check_lists(context, base_url, video, playlist_path, allowed=allowed)
        progress["phase"] = "remaining_recipient_playback"
        second.goto(base_url + watch_path)
        remaining_playback = play_and_seek(second)
        owner.reload()
        expect(owner.locator("[data-grants-list] li")).to_have_count(1)
        expect(owner.locator("[data-grants-list]")).to_contain_text(second_email)
        return {
            "status": "passed",
            "phase": "complete",
            "video_id": video_id,
            "grants": "two recipients via owner UI; persisted",
            "matrix": "owner, two recipients, outsider, anonymous",
            "surfaces": "metadata/social/comments/HTML/poster/MP4/GET/HEAD/Range",
            "lists": "search/channel/public playlist and counts filtered",
            "recipient_playback": playback,
            "revocation": "new requests denied only to revoked recipient",
            "remaining_recipient_playback": remaining_playback,
            "viewports": ["1440x900", "390x844"],
        }
    finally:
        for context in reversed(contexts):
            context.close()


def main() -> int:
    try:
        base_url = validate_origin(
            os.environ.get("FOXTUBE_TEST_BASE_URL", "http://localhost:8080")
        )
    except ValueError:
        print("Deployment Chrome: failed; phase=origin_validation")
        return 1
    artifacts = Path(os.environ.get("FOXTUBE_BROWSER_ARTIFACTS", "/artifacts/ubuntu"))
    artifacts.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "failed",
        "base_url": base_url,
        "target": "already running deployment; ordinary users only",
        "phase": "source",
        "not_covered": [
            "bootstrap, OS packages, reboot and restart persistence",
            "staff roles and moderation",
            "resource capacity, HTTPS and non-Chrome browsers",
            "physical mobile device and pointer/touch seeking",
        ],
    }
    errors: list[str] = []
    page: Page | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="foxtube-ubuntu-browser-") as temporary:
            source = Path(temporary) / "synthetic-video.mp4"
            report["input"] = create_video(source)
            report["phase"] = "browser"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                report["browser"] = browser.version
                context = browser.new_context(
                    locale="ru-RU", viewport={"width": 1440, "height": 900}
                )
                try:
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.on(
                        "pageerror", lambda error: errors.append(type(error).__name__)
                    )
                    page.on(
                        "console",
                        lambda message: (
                            errors.append("console:error")
                            if message.type == "error"
                            else None
                        ),
                    )
                    report["phase"] = "registration_and_channel"
                    owner_name = f"Автор Ubuntu {uuid4().hex[:8]}"
                    register(page, base_url, owner_name)
                    page.get_by_role("link", name="Мой канал", exact=True).click()
                    expect(page.get_by_role("heading", level=1)).to_have_text(
                        owner_name
                    )
                    expect(
                        page.locator(".primary-nav .nav-item.is-active")
                    ).to_have_text("Мой канал")
                    report["registration_and_channel"] = "passed"
                    assert_no_overflow(page)
                    capture(page, artifacts / "channel-desktop.png")
                    mobile(page)
                    capture(page, artifacts / "channel-mobile.png")
                    page.set_viewport_size({"width": 1440, "height": 900})
                    report["phase"] = "public_upload"
                    video = upload(page, base_url, source, "public")
                    report["video_id"] = video["id"]
                    report["phase"] = "public_playback"
                    report["playback"] = play_and_seek(page)
                    duration = page.locator("[data-fox-player] video").evaluate(
                        "video => video.duration"
                    )
                    require(9.5 <= duration <= 10.5, "Wrong deployment video duration")
                    report["duration"] = duration
                    report["phase"] = "social"
                    report["social"] = check_social(
                        page, base_url, f"/watch/{video['id']}"
                    )
                    assert_no_overflow(page)
                    capture(page, artifacts / "watch-desktop.png")
                    mobile(page)
                    player_box = page.locator("[data-fox-player] video").bounding_box()
                    require(
                        player_box is not None and player_box["width"] >= 388,
                        "Deployment mobile player is narrower than the viewport",
                    )
                    capture(page, artifacts / "watch-mobile.png")
                    report["phase"] = "private_sharing"
                    report["privacy"] = {"status": "running"}
                    report["privacy"] = check_private(
                        browser,
                        page,
                        base_url,
                        source,
                        report["social"]["playlist"],
                        artifacts,
                        errors,
                        report["privacy"],
                    )
                    require(not errors, "Deployment browser reported JavaScript errors")
                    report["javascript_errors"] = errors
                    report["viewports"] = ["1440x900", "390x844"]
                    report["phase"] = "complete"
                    report["status"] = "passed"
                except Exception as error:
                    report["failure_type"] = type(error).__name__
                    report["javascript_errors"] = errors
                    if report.get("privacy", {}).get("status") == "running":
                        report["privacy"]["status"] = "failed"
                    if page is not None:
                        try:
                            capture(page, artifacts / "failure.png")
                        except Exception:
                            report["failure_screenshot"] = "unavailable"
                finally:
                    context.close()
                    browser.close()
    except Exception as error:
        report["status"] = "failed"
        report["failure_type"] = type(error).__name__
    finally:
        (artifacts / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    # Playwright exceptions may contain entered credentials. Print only safe fields.
    print(
        f"Ubuntu deployment Chrome: {report['status']}; phase={report['phase']}; "
        f"failure_type={report.get('failure_type', 'none')}"
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
