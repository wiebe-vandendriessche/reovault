"""Cache-bust without a build step. One `sha256` over the concatenated
bytes of the hand-written/vendored assets,
computed once at app startup. Injected into `/sw.js` (renaming its cache,
so `activate` deletes the old one) and appended as `?v=` on every asset URL
in the base template. A deploy that changes any asset invalidates exactly
the right thing, with no filename hashing and no manual version bump."""

from __future__ import annotations

import hashlib
from pathlib import Path

_ASSET_FILES = (
    "app.css",
    "app.js",
    "htmx.min.js",
    "icons.svg",
    "fonts/montserrat-latin-var.woff2",
    "icons/icon.svg",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/icon-maskable-512.png",
    "icons/wordmark-dark.png",
    "icons/wordmark-light.png",
)


def compute_asset_version(static_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in _ASSET_FILES:
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:8]
