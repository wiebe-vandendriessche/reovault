"""Screenshots of every dashboard page from a healthy demo instance, through
the Playwright browser container (see tests/e2e/conftest.py to start it).

    python3 scripts/screenshots.py review OUT_DIR   # every page, dark+light, desktop+phone
    python3 scripts/screenshots.py readme           # the README/docs set into img/ and docs/img/
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from demo_instance import Instance, demo_instance
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WS = os.environ.get("PLAYWRIGHT_WS", "ws://127.0.0.1:3123/")
VIEWPORTS = {"desktop": (1280, 720), "phone": (390, 844)}
PAGES = {
    "overview": "/",
    "footage": "/footage",
    "activity": "/activity",
    "cameras": "/cameras",
    "settings-backup": "/settings?tab=backup",
    "settings-alerts": "/settings?tab=alerts",
    "settings-web": "/settings?tab=web",
    "settings-system": "/settings?tab=system",
    "login": "/login",
}
README = ["overview", "footage", "activity", "cameras", "settings-backup", "settings-alerts"]


def _login(page: Page, demo: Instance) -> None:
    page.goto(f"{demo.url}/login")
    page.fill("#email", demo.email)
    page.fill("#password", demo.password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url)


def _shoot(page: Page, demo: Instance, name: str, out: Path, full: bool) -> None:
    page.goto(f"{demo.url}{PAGES[name]}")
    page.wait_for_load_state("networkidle")
    if name == "footage":  # the interesting part is a day's clips
        # The newest hour may hold a single clip yet; the one before is full.
        page.locator("[data-slot=collapsible-trigger]").nth(1).click()
    page.wait_for_timeout(300)  # let transitions settle
    page.screenshot(path=str(out), full_page=full)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["review", "readme"])
    p.add_argument("out", nargs="?", type=Path)
    args = p.parse_args()
    tools = ROOT / ".tools"
    restic = shutil.which("restic") or str(tools / "restic")
    rclone = shutil.which("rclone") or str(tools / "rclone")
    with demo_instance(restic, rclone) as demo, sync_playwright() as pw:
        browser = pw.chromium.connect(WS)
        if args.mode == "readme":
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720}, device_scale_factor=2
            )
            ctx.add_init_script("localStorage.setItem('rv-theme', 'dark')")
            page = ctx.new_page()
            _login(page, demo)
            for name in README:
                for folder in (ROOT / "img", ROOT / "docs" / "img"):
                    folder.mkdir(exist_ok=True)
                    _shoot(page, demo, name, folder / f"screenshot-{name}.png", full=False)
                print(f"screenshot-{name}.png")
        else:
            assert args.out, "review needs an output folder"
            args.out.mkdir(parents=True, exist_ok=True)
            for theme in ("dark", "light"):
                for vp, (w, h) in VIEWPORTS.items():
                    ctx = browser.new_context(viewport={"width": w, "height": h})
                    ctx.add_init_script(f"localStorage.setItem('rv-theme', '{theme}')")
                    page = ctx.new_page()
                    _shoot(page, demo, "login", args.out / f"login-{theme}-{vp}.png", True)
                    _login(page, demo)
                    for name in PAGES:
                        if name != "login":
                            _shoot(page, demo, name, args.out / f"{name}-{theme}-{vp}.png", True)
                    ctx.close()
            print(f"saved to {args.out}")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
