#!/usr/bin/env python3
"""One-off developer tool: rebuild reovault/web/static/icons.svg from Lucide
(ISC license). Not run at build/install time -- the sprite it produces is
committed, so the app never needs network access to render an icon.

Run with: uv run python scripts/build_icon_sprite.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

LUCIDE_VERSION = "0.544.0"
_BASE_URL = f"https://unpkg.com/lucide-static@{LUCIDE_VERSION}/icons"

# Every icon name referenced by a template's icon()/explain() call. Keep
# alphabetical; add here and re-run this script rather than editing
# icons.svg by hand.
ICON_NAMES = [
    "activity",
    "arrow-right",
    "calendar-clock",
    "calendar-range",
    "cctv",
    "chevron-down",
    "chevron-left",
    "chevron-right",
    "circle-alert",
    "circle-check",
    "circle-help",
    "clock",
    "download",
    "eye",
    "eye-off",
    "file-video",
    "hard-drive",
    "key-round",
    "list-checks",
    "loader-circle",
    "log-out",
    "menu",
    "moon",
    "play",
    "plus",
    "radar",
    "refresh-cw",
    "rotate-ccw",
    "settings",
    "shield-check",
    "sun",
    "triangle-alert",
    "video",
    "wifi-off",
    "x",
]

_INNER_RE = re.compile(r"<svg\b[^>]*>(.*)</svg>", re.DOTALL)
_LICENSE_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)

_OUT_PATH = Path(__file__).parent.parent / "reovault" / "web" / "static" / "icons.svg"


def _strip_to_symbol(name: str, raw_svg: str) -> str:
    """Lucide ships each icon as a full <svg ...>...</svg> with its own
    xmlns/width/height/stroke attrs. A sprite needs just the inner paths
    wrapped in a <symbol id> so <use href="#name"> can pick them up and
    inherit color/size from CSS (stroke: currentColor already set at the
    <svg> root in app.css's .icon rule, not repeated per symbol)."""
    without_license = _LICENSE_RE.sub("", raw_svg, count=1)
    match = _INNER_RE.search(without_license)
    if not match:
        raise ValueError(f"could not parse {name}.svg: no <svg>...</svg> wrapper found")
    inner = match.group(1).strip()
    return f'  <symbol id="{name}" viewBox="0 0 24 24">\n    {inner}\n  </symbol>'


def main() -> int:
    symbols = []
    for name in ICON_NAMES:
        url = f"{_BASE_URL}/{name}.svg"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name}: {exc}", file=sys.stderr)
            return 1
        symbols.append(_strip_to_symbol(name, raw))

    sprite = (
        f"<!-- Lucide v{LUCIDE_VERSION} (ISC license, https://lucide.dev). "
        "Regenerate with scripts/build_icon_sprite.py; do not hand-edit. -->\n"
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
        + "\n".join(symbols)
        + "\n</svg>\n"
    )
    _OUT_PATH.write_text(sprite)
    print(f"[ OK ] wrote {_OUT_PATH} ({len(sprite)} bytes, {len(ICON_NAMES)} icons)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
