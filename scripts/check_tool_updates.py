"""Report reolink-cli, restic or rclone releases newer than the pinned ones.
Dependabot can't see these (they're GitHub release downloads, not packages).

    python3 scripts/check_tool_updates.py                # print differences
    python3 scripts/check_tool_updates.py --open-issues  # also open an issue each (needs `gh`)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

from fetch_tools import pinned

REPOS = {"reolink-cli": "reolink/reolink-cli", "restic": "restic/restic", "rclone": "rclone/rclone"}

CHECKLIST = """A new {tool} release is out: {tag} (pinned: {pinned}).
Changelog: https://github.com/{repo}/releases/tag/{tag}

To update:
- [ ] Read the changelog for anything that touches how ReoVault calls it.
- [ ] Bump the `ARG` in the Dockerfile and the default in `reovault/config.py` (`ToolsConfig`).
- [ ] `python3 scripts/fetch_tools.py .tools` and run the full test suite.
- [ ] For reolink-cli: re-check search and batch download on real hardware (docs/compatibility.md).
"""


def latest(repo: str) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URL
        tag: str = json.load(resp)["tag_name"]
        return tag


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--open-issues", action="store_true")
    args = p.parse_args()
    outdated = 0
    for tool, version in pinned().items():
        tag = latest(REPOS[tool])
        if tag.lstrip("v") == version:
            print(f"{tool}: {version} is current")
            continue
        outdated += 1
        print(f"{tool}: pinned {version}, latest {tag}")
        if not args.open_issues:
            continue
        title = f"Update {tool} to {tag}"
        found = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                f'in:title "{title}"',
                "--json",
                "title",
                "--jq",
                ".[].title",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if title in found:
            print(f"  issue already open: {title}")
            continue
        body = CHECKLIST.format(tool=tool, tag=tag, pinned=version, repo=REPOS[tool])
        subprocess.run(["gh", "issue", "create", "--title", title, "--body", body], check=True)
    return 0 if args.open_issues else min(outdated, 1)


if __name__ == "__main__":
    sys.exit(main())
