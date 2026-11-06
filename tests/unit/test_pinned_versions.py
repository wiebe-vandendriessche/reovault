"""The tool versions ReoVault checks at startup must be the ones the image
ships: the Dockerfile ARGs are the source of truth (scripts/fetch_tools.py)."""

import sys
from pathlib import Path

from reovault.config import ToolsConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fetch_tools import pinned  # noqa: E402


def test_config_defaults_match_the_dockerfile(monkeypatch):
    for var in ("REOLINK_CLI_VERSION", "RESTIC_RELEASE", "RCLONE_RELEASE"):
        monkeypatch.delenv(var, raising=False)
    versions = pinned()
    tools = ToolsConfig()
    assert tools.reolink_cli_version == versions["reolink-cli"]
    assert tools.restic_version == versions["restic"]
    assert tools.rclone_version == versions["rclone"]


def test_the_playwright_client_matches_the_browser_image():
    import re

    root = Path(__file__).resolve().parents[2]
    pin = re.search(r'"playwright==([\d.]+)"', (root / "pyproject.toml").read_text())
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    assert pin
    tags = set(re.findall(r"playwright:v([\d.]+)-", ci)) | set(
        re.findall(r"playwright@([\d.]+)", ci)
    )
    assert tags == {pin.group(1)}
