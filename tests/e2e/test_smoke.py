"""Every page loads, logged in, on a desktop and a phone, without a single
console error, page error, CSP violation (Chromium reports those as console
errors) or failed API call."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = pytest.mark.e2e

PAGES = [
    "/",
    "/footage",
    "/activity",
    "/cameras",
    "/settings",
    "/settings?tab=backup",
    "/settings?tab=recovery",
    "/settings?tab=alerts",
]
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


def test_a_range_download_and_a_run_detail(browser: Browser, demo) -> None:  # noqa: ANN001
    context = browser.new_context(viewport=VIEWPORTS["desktop"], accept_downloads=True)
    page = context.new_page()
    problems = _watch(page)
    _login(page, demo)
    page.goto(f"{demo.url}/footage")
    page.get_by_role("button", name="Download", exact=True).click()
    page.get_by_role("button", name="Last 7 days").click()
    expect(page.get_by_text(re.compile(r"\d+ clips?, "))).to_be_visible()  # the live preview
    with page.expect_download() as download:
        page.get_by_role("link", name="Download zip").click()
    assert download.value.suggested_filename.endswith(".zip")
    page.goto(f"{demo.url}/activity")
    page.locator("a[href^='/activity/runs/']:visible").first.click()
    page.wait_for_url("**/activity/runs/*")
    expect(page.get_by_text("Batches", exact=True)).to_be_visible()
    page.goto(f"{demo.url}/settings?tab=destinations")
    expect(page.get_by_role("button", name="Connect")).to_be_visible()
    context.close()
    assert problems == []


def test_first_run_setup_creates_the_login(browser: Browser, fresh) -> None:  # noqa: ANN001
    """A fresh install: every page leads to /setup, the logged code finishes it,
    and then the same password signs in again."""
    context = browser.new_context(viewport=VIEWPORTS["desktop"])
    page = context.new_page()
    problems = _watch(page)
    page.goto(f"{fresh.url}/")
    page.wait_for_url("**/setup")
    page.fill("#code", "AAAA-BBBB")
    page.fill("#email", "me@home.example")
    page.fill("#password", "a long enough passphrase")
    page.fill("#again", "a long enough passphrase")
    page.click("button[type=submit]")
    assert page.get_by_text("setup code is wrong").is_visible()  # a wrong code is refused
    page.fill("#code", fresh.setup_code())
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/setup" not in url)
    assert page.locator("h1").first.is_visible()
    # A second browser signs in with what was just chosen.
    other = browser.new_context(viewport=VIEWPORTS["desktop"])
    page2 = other.new_page()
    page2.goto(f"{fresh.url}/login")
    page2.fill("#email", "me@home.example")
    page2.fill("#password", "a long enough passphrase")
    page2.click("button[type=submit]")
    page2.wait_for_url(lambda url: "/login" not in url)
    other.close()
    context.close()
    # 403 (wrong code) is the one expected API error here.
    assert [p for p in problems if "403" not in p and "Failed to load resource" not in p] == []
