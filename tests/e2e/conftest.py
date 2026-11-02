"""Browser tests: a demo instance on the host, Chromium in the official
Playwright container (`playwright run-server`), driven from here.

    docker run -d --rm --name pw --network host --init \
      mcr.microsoft.com/playwright:v1.64.1-noble \
      npx -y playwright@1.64.1 run-server --port 3123 --host 127.0.0.1
    PATH=$PWD/.tools:$PATH uv run pytest -m e2e
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from demo_instance import Instance, demo_instance  # noqa: E402

WS = os.environ.get("PLAYWRIGHT_WS", "ws://127.0.0.1:3123/")


@pytest.fixture(autouse=True)
def isolated_settings_sources() -> None:
    """The demo instance brings its own config; skip the root conftest's."""


@pytest.fixture(scope="session")
def demo() -> Iterator[Instance]:
    restic, rclone = shutil.which("restic"), shutil.which("rclone")
    if restic is None or rclone is None:
        pytest.fail("restic and rclone must be on PATH (python3 scripts/fetch_tools.py .tools)")
    if not (
        Path(__file__).resolve().parents[2] / "reovault" / "web" / "dist" / "index.html"
    ).exists():
        pytest.fail("the dashboard isn't built into reovault/web/dist (see CONTRIBUTING.md)")
    with demo_instance(restic, rclone) as instance:
        yield instance


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            b = p.chromium.connect(WS, timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"no Playwright server at {WS} ({exc}); see tests/e2e/conftest.py")
        yield b
        b.close()
