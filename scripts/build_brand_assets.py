"""Make the dashboard's brand files from the three SVGs in `img/`, the only
brand sources:

    img/reovault-bg.svg       wordmark, white "Vault" on the dark surface (#000814)
    img/reovault-dark-bg.svg  wordmark, dark "Vault", transparent (for light pages)
    img/reovault-icon.svg     the icon

Writes into `dashboard/static/icons/`:

* `wordmark-dark.svg` and `wordmark-light.svg`: the wordmarks (for the dark and
  the light theme) with the empty margin of the 400x200 artboard cropped off
  (at the navbar's 28px height the uncropped art renders as a smear). Same
  paths, only the viewBox changes.
* `icon.svg`: the icon, as it is.
* PNGs for what can't take an SVG (home-screen icons, the PWA manifest),
  rendered from the icon SVG by Chromium: `icon-192.png`, `icon-512.png`,
  `apple-touch-icon-180.png` and `icon-maskable-512.png` (the last two on the
  dark surface, because iOS and maskable icons must be opaque).

Not run at build or install time; run by hand when the art changes and commit
the result. It needs the Playwright browser container (see CONTRIBUTING.md):

    PATH=$PWD/.tools:$PATH uv run --with pillow python scripts/build_brand_assets.py
"""

from __future__ import annotations

import base64
import io
import os
import re
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

_ROOT = Path(__file__).resolve().parent.parent
_IMG = _ROOT / "img"
_OUT = _ROOT / "dashboard/static/icons"
_WS = os.environ.get("PLAYWRIGHT_WS", "ws://127.0.0.1:3123/")
_SURFACE = "#000814"
_MARGIN = 0.04  # of the ink width, added back around the cropped wordmark
_ARTBOARD = 400  # the wordmark SVGs are 400 x 200 user units; the render scale below
_SCALE = 5

# (source, destination)
_WORDMARKS = [
    (_IMG / "reovault-bg.svg", _OUT / "wordmark-dark.svg"),
    (_IMG / "reovault-dark-bg.svg", _OUT / "wordmark-light.svg"),
]


def _render(page: Page, svg: bytes, width: int, height: int, background: str | None) -> Image.Image:
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    page.set_viewport_size({"width": width, "height": height})
    page.set_content(
        f'<body style="margin:0;background:{background or "transparent"}">'
        f'<img src="{uri}" style="width:{width}px;height:{height}px;display:block">'
    )
    page.wait_for_function("document.images[0].complete")
    png = page.screenshot(omit_background=background is None)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _ink_box(page: Page, svg: bytes) -> tuple[float, float, float, float]:
    """The wordmark's ink in user units: render it transparent and take the
    bounding box of what isn't see-through."""
    im = _render(page, svg, _ARTBOARD * _SCALE, _ARTBOARD // 2 * _SCALE, None)
    box = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    assert box is not None, "the wordmark is empty"
    return tuple(v / _SCALE for v in box)  # type: ignore[return-value]


def _crop(svg: str, box: tuple[float, float, float, float]) -> str:
    left, top, right, bottom = box
    pad = (right - left) * _MARGIN
    x, y, w, h = left - pad, top - pad, right - left + 2 * pad, bottom - top + 2 * pad
    svg = re.sub(r'viewBox="[^"]*"', f'viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}"', svg, count=1)
    # Inkscape writes the artboard in mm; the cropped art is sized in plain px.
    svg = re.sub(r'\n(\s+)width="[^"]*"', f'\n\\1width="{w:.0f}"', svg, count=1)
    return re.sub(r'\n(\s+)height="[^"]*"', f'\n\\1height="{h:.0f}"', svg, count=1)


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.connect(_WS)
        page = browser.new_page()
        # Measured on the transparent wordmark: the one with a background fills
        # its whole artboard. Both have the same letters in the same place.
        box = _ink_box(page, (_IMG / "reovault-dark-bg.svg").read_bytes())
        for src, dest in _WORDMARKS:
            dest.write_text(_crop(src.read_text(), box))
            print(f"{src.relative_to(_ROOT)} -> {dest.relative_to(_ROOT)}")

        icon = (_IMG / "reovault-icon.svg").read_bytes()
        (_OUT / "icon.svg").write_bytes(icon)
        print("img/reovault-icon.svg -> dashboard/static/icons/icon.svg")
        # (file, size, background, share of the canvas the icon fills)
        for name, size, background, fill in [
            ("icon-192.png", 192, None, 1.0),
            ("icon-512.png", 512, None, 1.0),
            ("apple-touch-icon-180.png", 180, _SURFACE, 0.8),
            ("icon-maskable-512.png", 512, _SURFACE, 0.66),  # inside the 80% safe zone
        ]:
            inner = round(size * fill)
            art = _render(page, icon, inner, inner, None)
            canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
            canvas.alpha_composite(art, ((size - inner) // 2, (size - inner) // 2))
            canvas.save(_OUT / name)
            print(f"-> dashboard/static/icons/{name}")
        browser.close()


if __name__ == "__main__":
    main()
