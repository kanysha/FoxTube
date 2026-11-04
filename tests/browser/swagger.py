"""Exercise the actual Swagger Try it out controls, including a binary upload."""

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Page, expect


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_swagger_upload(
    page: Page, base_url: str, source: Path, artifacts: Path
) -> dict[str, Any]:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/docs")
    create = page.locator(".opblock-post").filter(
        has=page.locator('[data-path="/api/v1/videos"]')
    )
    create.locator(".opblock-summary").click()
    create.get_by_role("button", name="Try it out", exact=True).click()
    create.locator("textarea.body-param__text").fill(
        json.dumps(
            {
                "title": f"Swagger Chrome {uuid4().hex[:10]}",
                "description": "Настоящий файл через Swagger Try it out.",
                "visibility": "public",
                "expected_size": source.stat().st_size,
            },
            ensure_ascii=False,
        )
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/api/v1/videos"
        ),
        timeout=30000,
    ) as created:
        create.get_by_role("button", name="Execute", exact=True).click()
    response = created.value
    require(response.status == 201, "Swagger metadata request failed")
    video_id = response.json()["id"]
    upload = page.locator(".opblock-put").filter(
        has=page.locator('[data-path="/api/v1/videos/{video_id}/source"]')
    )
    upload.locator(".opblock-summary").click()
    upload.get_by_role("button", name="Try it out", exact=True).click()
    upload.locator('input[placeholder="video_id"]').fill(video_id)
    upload.locator('input[type="file"]').set_input_files(source)
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and urlsplit(response.url).path == f"/api/v1/videos/{video_id}/source"
        ),
        timeout=60000,
    ) as uploaded:
        upload.get_by_role("button", name="Execute", exact=True).click()
    require(uploaded.value.status == 202, "Swagger binary upload failed")
    # Swagger's expanded curl example contains the live CSRF token.
    create.locator(".opblock-summary").click()
    upload.locator(".opblock-summary").click()
    page.screenshot(path=artifacts / "swagger-upload.png", full_page=True)

    deadline = time.monotonic() + 120
    status = ""
    while time.monotonic() < deadline:
        metadata = page.request.get(f"{base_url}/api/v1/videos/{video_id}")
        require(metadata.status == 200, "Swagger video metadata became unavailable")
        status = metadata.json()["status"]
        metadata.dispose()
        require(status != "FAILED", "Swagger video processing failed")
        if status == "READY":
            break
        page.wait_for_timeout(500)
    require(status == "READY", "Swagger video did not become READY")
    page.goto(f"{base_url}/watch/{video_id}")
    expect(page.locator("video")).to_be_visible()
    page.locator("video").evaluate("video => video.play()")
    page.wait_for_function(
        "() => document.querySelector('video').currentTime > 1", timeout=30000
    )
    playing_time = page.locator("video").evaluate("video => video.currentTime")
    page.locator("video").evaluate("video => video.pause()")
    return {
        "status": "passed",
        "metadata": 201,
        "binary_upload": 202,
        "processing": status,
        "playing_time": playing_time,
    }
