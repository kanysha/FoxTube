"""Private sharing through the real owner UI and separate Chrome sessions."""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Browser, BrowserContext, Page, expect


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def register(page: Page, base_url: str, name: str) -> str:
    email = f"privacy-{uuid4().hex}@example.com"
    page.goto(f"{base_url}/register")
    page.get_by_label("Имя", exact=True).fill(name)
    page.get_by_label("Email", exact=True).fill(email)
    page.get_by_label("Пароль", exact=True).fill(
        "Disposable-private-browser-passphrase-42!"
    )
    page.get_by_role("button", name="Создать аккаунт", exact=True).click()
    page.wait_for_url(f"{base_url}/account")
    return email


def check_media(
    context: BrowserContext, base_url: str, video_id: str, *, allowed: bool
) -> None:
    for suffix in ("", "/social", "/comments"):
        response = context.request.get(f"{base_url}/api/v1/videos/{video_id}{suffix}")
        require(
            response.status == (200 if allowed else 404),
            f"Private video API ACL failed for {suffix or 'metadata'}",
        )
        response.dispose()
    watch = context.request.get(f"{base_url}/watch/{video_id}")
    require(watch.status == (200 if allowed else 404), "Private watch HTML ACL failed")
    watch.dispose()
    for kind in ("playback", "poster"):
        url = f"{base_url}/media/videos/{video_id}/{kind}"
        for method, headers, expected in (
            ("GET", {}, 200),
            ("HEAD", {}, 200),
            ("GET", {"Range": "bytes=0-63"}, 206),
        ):
            response = context.request.fetch(url, method=method, headers=headers)
            require(
                response.status == (expected if allowed else 404),
                f"Private {kind} {method} {'Range' if headers else ''} ACL failed",
            )
            if allowed:
                require(
                    "no-store" in response.headers.get("cache-control", ""),
                    "Private media response permits caching",
                )
                if headers:
                    require(
                        response.headers.get("content-range", "").startswith(
                            "bytes 0-63/"
                        )
                        and len(response.body()) == 64,
                        "Private media did not serve the requested range",
                    )
                if method == "HEAD":
                    require(not response.body(), "Private media HEAD returned bytes")
            response.dispose()


def check_lists(
    context: BrowserContext,
    base_url: str,
    video: dict[str, Any],
    playlist_path: str,
    *,
    allowed: bool,
) -> None:
    video_id = video["id"]
    channel_id = video["channel_id"]
    for path, params in (
        ("/api/v1/videos", {"q": video["title"]}),
        (f"/api/v1/channels/{channel_id}/videos", {}),
    ):
        response = context.request.get(base_url + path, params=params)
        require(response.status == 200, "Private video list request failed")
        data = response.json()
        require(
            any(item["id"] == video_id for item in data["items"]) == allowed,
            "Private video leaked or disappeared from an ACL-filtered list",
        )
        if params:
            require(data["total"] == int(allowed), "Search count leaks private video")
        else:
            channel = context.request.get(f"{base_url}/api/v1/channels/{channel_id}")
            require(
                channel.status == 200
                and channel.json()["videos_count"] == data["total"],
                "Channel count differs from the accessible videos",
            )
            channel.dispose()
        response.dispose()
    response = context.request.get(f"{base_url}/api/v1{playlist_path}")
    require(response.status == 200, "Public playlist is unavailable")
    data = response.json()
    require(
        any(item["id"] == video_id for item in data["videos"]["items"]) == allowed,
        "Public playlist bypasses private video ACL",
    )
    require(
        data["playlist"]["videos_count"] == data["videos"]["total"] == 1 + int(allowed),
        "Public playlist count leaks private video",
    )
    response.dispose()


def play_and_seek(page: Page) -> dict[str, float]:
    player = page.locator("[data-fox-player] video")
    expect(player).to_be_visible()
    page.wait_for_function(
        """() => {
          const video = document.querySelector('[data-fox-player] video');
          return video && video.readyState >= 2 && !video.error;
        }""",
        timeout=30000,
    )
    # A real gesture permits unmuted playback in a fresh recipient context.
    page.locator("[data-fox-center]").click()
    page.wait_for_function(
        "() => document.querySelector('[data-fox-player] video').currentTime > 1",
        timeout=20000,
    )
    playing = player.evaluate("video => video.currentTime")
    player.evaluate("video => { video.pause(); video.currentTime = 7; }")
    page.wait_for_function(
        """() => {
          const video = document.querySelector('[data-fox-player] video');
          return !video.seeking && video.readyState >= 2 &&
            Math.abs(video.currentTime - 7) < 0.6 && !video.error;
        }""",
        timeout=20000,
    )
    player.evaluate("video => video.play()")
    page.wait_for_function(
        "() => document.querySelector('[data-fox-player] video').currentTime > 8",
        timeout=20000,
    )
    after_seek = player.evaluate("video => video.currentTime")
    player.evaluate("video => video.pause()")
    return {"playing_time": playing, "playing_after_seek": after_seek}


def capture(page: Page, path: Path) -> None:
    # Capture the top viewport so fixed navigation stays at the top of the
    # full-page image after Playwright scrolled a form control into view.
    page.evaluate("() => { document.activeElement?.blur(); window.scrollTo(0, 0); }")
    page.wait_for_function("() => window.scrollY === 0")
    page.screenshot(path=path, full_page=True)


def check_privacy(
    browser: Browser,
    owner: Page,
    base_url: str,
    source: Path,
    playlist_path: str,
    artifacts: Path,
    errors: list[str],
) -> dict[str, Any]:
    contexts: list[BrowserContext] = []

    def new_page() -> Page:
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="ru-RU"
        )
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        return page

    try:
        first, second, outsider, guest = [new_page() for _ in range(4)]
        first_email = register(first, base_url, "Первый приглашённый")
        second_email = register(second, base_url, "Второй приглашённый")
        outsider_email = register(outsider, base_url, "Посторонний зритель")
        owner.set_viewport_size({"width": 1440, "height": 900})
        owner.goto(f"{base_url}/upload")
        owner.locator('input[type="file"]').set_input_files(source)
        owner.locator('[name="title"]').fill(
            f"Приватный круг Chrome {uuid4().hex[:10]}"
        )
        owner.locator('[name="description"]').fill("Доступ только двум приглашённым.")
        owner.locator('[name="visibility"]').select_option("private")
        with owner.expect_response(
            lambda response: (
                response.request.method == "POST"
                and urlsplit(response.url).path == "/api/v1/videos"
            )
        ) as created:
            owner.locator('form:has(input[type="file"]) button[type="submit"]').click()
        require(created.value.status == 201, "Private UI upload metadata failed")
        video = created.value.json()
        video_id = video["id"]
        watch_path = f"/watch/{video_id}"
        link = owner.locator(f'[data-upload-watch][href="{watch_path}"]')
        expect(link).to_be_visible(timeout=120000)
        link.click()
        owner.wait_for_url(base_url + watch_path)
        expect(owner.locator(".watch-heading .privacy-badge")).to_have_text("Приватное")
        expect(owner.locator("[data-grants-empty]")).to_have_text(
            "Разрешения пока не выданы."
        )
        for page in (first, second, outsider, guest):
            check_media(page.context, base_url, video_id, allowed=False)

        for email in (first_email, second_email):
            owner.get_by_label("Email пользователя", exact=True).fill(email)
            owner.get_by_role("button", name="Разрешить просмотр", exact=True).click()
            expect(owner.locator("[data-grants-list]")).to_contain_text(email)
        owner.reload()
        expect(owner.locator("[data-grants-list] li")).to_have_count(2)
        for email in (first_email, second_email):
            expect(owner.locator("[data-grants-list]")).to_contain_text(email)

        owner.locator("[data-save-toggle]").click()
        owner.locator("[data-save-playlist] select").select_option(
            playlist_path.rsplit("/", 1)[1]
        )
        owner.locator("[data-save-playlist] button[type=submit]").click()
        expect(owner.locator("[data-save-result]")).to_be_visible()

        for page, allowed in (
            (owner, True),
            (first, True),
            (second, True),
            (outsider, False),
            (guest, False),
        ):
            check_media(page.context, base_url, video_id, allowed=allowed)
            check_lists(page.context, base_url, video, playlist_path, allowed=allowed)

        # A recipient may view the video but cannot widen its audience.
        first.goto(base_url + watch_path)
        expect(first.locator("[data-video-owner]")).to_have_count(0)
        csrf = first.locator('meta[name="csrf-token"]').get_attribute("content")
        require(bool(csrf), "Recipient session has no CSRF token")
        for method, suffix, data in (
            ("POST", "/grants", {"email": outsider_email}),
            ("PATCH", "", {"visibility": "public"}),
        ):
            response = first.context.request.fetch(
                f"{base_url}/api/v1/videos/{video_id}{suffix}",
                method=method,
                headers={"Origin": base_url, "X-CSRF-Token": csrf or ""},
                data=data,
            )
            require(response.status == 404, "Recipient changed the owner's audience")
            response.dispose()
        grants = first.context.request.get(
            f"{base_url}/api/v1/videos/{video_id}/grants"
        )
        require(grants.status == 404, "Recipient can enumerate the owner's grants")
        grants.dispose()
        playback = play_and_seek(first)
        capture(first, artifacts / "private-viewer-desktop.png")
        # Pin a real granted private video and retain its decoder across server pages.
        first.locator("[data-fox-player] video").evaluate(
            "video => { window.__privateMini = {video, document}; }"
        )
        first.locator("[data-fox-mini]").click()
        first.locator('.brand[href="/"]').click()
        first.wait_for_url(base_url + "/")
        expect(first.locator("[data-miniplayer]")).to_be_visible()
        first.locator("[data-mini-restore]").click()
        first.wait_for_url(base_url + watch_path)
        expect(first.locator("#main-content [data-fox-player] video")).to_be_visible()
        require(
            first.evaluate(
                "() => window.__privateMini.document === document && "
                "document.querySelector('[data-fox-player] video') "
                "=== window.__privateMini.video"
            ),
            "Private mini navigation replaced the video",
        )
        first.locator("[data-fox-mini]").click()
        capture(owner, artifacts / "private-owner-desktop.png")
        owner.set_viewport_size({"width": 390, "height": 844})
        owner.wait_for_function(
            """() => {
              const sidebar = document.querySelector('#site-sidebar');
              return document.body.dataset.sidebarOpen === 'false' &&
                sidebar.getBoundingClientRect().right <= 1;
            }"""
        )
        require(
            owner.evaluate(
                "() => document.documentElement.scrollWidth <= window.innerWidth"
            ),
            "Private owner controls overflow on mobile",
        )
        capture(owner, artifacts / "private-owner-mobile.png")

        owner.get_by_role("button", name=f"Отозвать доступ: {first_email}").click()
        expect(owner.locator("[data-grants-list] li")).to_have_count(1)
        expect(owner.locator("[data-grants-list]")).not_to_contain_text(first_email)
        expect(owner.locator("[data-grants-list]")).to_contain_text(second_email)
        # The next internal transition rechecks the grant and releases buffered media.
        first.locator('.brand[href="/"]').click()
        first.wait_for_url(base_url + "/")
        expect(first.locator("[data-miniplayer]")).to_be_hidden()
        expect(first.locator("[data-mini-title]")).to_have_text("")
        first.wait_for_function(
            "() => window.__privateMini.video.paused && "
            "!window.__privateMini.video.isConnected && "
            "window.__privateMini.video.readyState === 0 && "
            "window.__privateMini.video.networkState === 0 && "
            "window.__privateMini.video.buffered.length === 0 && "
            "!window.__privateMini.video.getAttribute('src') && "
            "!window.__privateMini.video.getAttribute('poster') && "
            "!window.__privateMini.video.querySelector('source,track')"
        )
        # Buffered bytes in the already-open player cannot be recalled. These
        # checks always issue new requests after the revoke has completed.
        for page, allowed in ((first, False), (second, True)):
            check_media(page.context, base_url, video_id, allowed=allowed)
            check_lists(page.context, base_url, video, playlist_path, allowed=allowed)
        second.goto(base_url + watch_path)
        remaining_playback = play_and_seek(second)
        first.goto(f"{base_url}/channels/{video['channel_id']}")
        expect(first.locator(f'a[href="{watch_path}"]')).to_have_count(0)
        second.goto(f"{base_url}/channels/{video['channel_id']}")
        expect(second.locator(f'a[href="{watch_path}"]').first).to_be_visible()
        outsider.goto(base_url + playlist_path)
        expect(outsider.locator(f'a[href="{watch_path}"]')).to_have_count(0)
        second.goto(base_url + playlist_path)
        expect(second.locator(f'a[href="{watch_path}"]').first).to_be_visible()
        owner.reload()
        expect(owner.locator("[data-grants-list] li")).to_have_count(1)
        expect(owner.locator("[data-grants-list]")).to_contain_text(second_email)
        return {
            "status": "passed",
            "video_id": video_id,
            "upload": "private via UI; real FFmpeg READY",
            "grants": "two recipients via UI; persisted after reload",
            "matrix": (
                "owner, two recipients, outsider and guest; "
                "metadata/social/comments/HTML"
            ),
            "media": "poster and MP4 GET/HEAD/Range through Nginx; no-store",
            "lists": "search/channel counts/public playlist filtered by viewer ACL",
            "owner_control": (
                "recipient cannot list grants, grant access or make public"
            ),
            "recipient_playback": playback,
            "revocation": (
                "first recipient denied new requests; second still plays and seeks"
            ),
            "remaining_recipient_playback": remaining_playback,
            "miniplayer_revocation": (
                "same private decoder across home/restore; next navigation after "
                "revoke closes mini and clears source, tracks and title"
            ),
            "viewports": ["1440x900", "390x844"],
        }
    finally:
        for context in reversed(contexts):
            context.close()
