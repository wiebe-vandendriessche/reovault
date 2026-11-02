"""Every page loads, logged in, on a desktop and a phone, without a single
console error, page error, CSP violation (Chromium reports those as console
errors) or failed API call."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page

pytestmark = pytest.mark.e2e

PAGES = ["/", "/footage", "/activity", "/cameras", "/settings", "/settings?tab=alerts"]
VIEWPORTS = {"desktop": {"width": 1280, "height": 720}, "phone": {"width": 390, "height": 844}}


def _watch(page: Page) -> list[str]:
    problems: list[str] = []
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))

    def response(r) -> None:  # noqa: ANN001
        if "/api/" in r.url and not r.url.endswith("/api/v1/events") and r.status >= 400:
            problems.append(f"{r.status} {r.request.method} {r.url}")

    page.on("response", response)
    return problems


def _login(page: Page, demo) -> None:  # noqa: ANN001
    page.goto(f"{demo.url}/login")
    page.fill("#email", demo.email)
    page.fill("#password", demo.password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url)


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_every_page_loads_cleanly(browser: Browser, demo, viewport) -> None:  # noqa: ANN001
    context = browser.new_context(viewport=VIEWPORTS[viewport], accept_downloads=True)
    page = context.new_page()
    problems = _watch(page)
    _login(page, demo)
    for path in PAGES:
        page.goto(f"{demo.url}{path}")
        page.wait_for_load_state("networkidle")
        assert page.locator("h1").first.is_visible(), path
    context.close()
    assert problems == []


def test_a_clip_downloads_from_footage(browser: Browser, demo) -> None:  # noqa: ANN001
    context = browser.new_context(viewport=VIEWPORTS["desktop"], accept_downloads=True)
    page = context.new_page()
    problems = _watch(page)
    _login(page, demo)
    page.goto(f"{demo.url}/footage")
    page.locator("[data-slot=collapsible-trigger]").first.click()
    with page.expect_download() as download:
        page.get_by_role("link", name="Download clip").first.click()
    assert download.value.suggested_filename.endswith(".mp4")
    context.close()
    assert problems == []
