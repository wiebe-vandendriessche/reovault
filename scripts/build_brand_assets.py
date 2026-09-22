"""Crop the source wordmark PNGs down to their ink plus a small even margin.
The two `img/reovault-*-bg.png` source files are hand-produced with ~30% of
baked-in whitespace on every side -- fine as a standalone logo image, wrong
at `height: 30px` in a 56px navbar, where it renders as an 8px-tall smear.
Not run at build/install time; run by hand with
`uv run --with pillow python scripts/build_brand_assets.py` whenever the
source art changes, and commit the result.

Never touches img/reovault.png: that's the transparent wordmark and must
never be used in the navbar, which assumes an opaque background (see
app.css's .topbar comment).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

_ROOT = Path(__file__).resolve().parent.parent
_MARGIN_FRACTION = 0.06  # even margin added back around the cropped ink box

_SOURCES = [
    (_ROOT / "img" / "reovault-bg.png", _ROOT / "reovault/web/static/icons/wordmark-light.png"),
    (
        _ROOT / "img" / "reovault-dark-bg.png",
        _ROOT / "reovault/web/static/icons/wordmark-dark.png",
    ),
]


def _ink_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    bbox = ImageChops.difference(rgb, bg).getbbox()
    if bbox is None:
        raise ValueError("image is a single solid color; nothing to crop to")
    return bbox


def _crop_with_margin(im: Image.Image) -> Image.Image:
    left, top, right, bottom = _ink_bbox(im)
    w, h = right - left, bottom - top
    margin = round(max(w, h) * _MARGIN_FRACTION)
    left = max(0, left - margin)
    top = max(0, top - margin)
    right = min(im.width, right + margin)
    bottom = min(im.height, bottom + margin)
    return im.crop((left, top, right, bottom))


def main() -> None:
    for src, dest in _SOURCES:
        im = Image.open(src)
        cropped = _crop_with_margin(im)
        dest.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(dest)
        print(f"{src.relative_to(_ROOT)} -> {dest.relative_to(_ROOT)}  {im.size} -> {cropped.size}")


if __name__ == "__main__":
    main()
