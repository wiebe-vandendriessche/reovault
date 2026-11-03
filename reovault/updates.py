"""Is a newer ReoVault out? One GET to GitHub's releases API a day.

Only the version number comes back; nothing about this server goes out. A
development build (`git describe` output, `dev`) never reports an update.
"""

from __future__ import annotations

import json
import re
import urllib.request

RELEASES_URL = "https://api.github.com/repos/wiebe-vandendriessche/reovault/releases/latest"
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse(version: str) -> tuple[int, int, int] | None:
    m = _VERSION.fullmatch(version.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def latest_release(timeout: float = 10) -> str | None:
    """The newest release tag, or None when it can't be read."""
    req = urllib.request.Request(
        RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "reovault"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            tag = json.load(resp).get("tag_name")
    except (OSError, ValueError):
        return None
    return tag if isinstance(tag, str) and parse(tag) else None


def newer(current: str, latest: str | None) -> str | None:
    """`latest` when it is a newer release than `current`, else None."""
    have, want = parse(current), parse(latest or "")
    return latest if have and want and want > have else None
