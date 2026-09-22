"""sha256 pins for every vendored, hand-fetched static asset. The point
isn't to forbid updating a vendored asset -- it's to force that update to
be a deliberate, reviewed diff (new hash, new byte count, ideally a version
bump in the header comment) rather than something that drifts quietly, e.g.
a build step re-fetching a "latest" URL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "reovault" / "web" / "static"

# (path relative to static/, expected sha256, expected byte size)
_PINNED_ASSETS = [
    (
        "htmx.min.js",
        "a70a6871db30c99968e918ed99b5b16633834c46939559d094f3c90d9d3a8a10",
        None,  # size not pinned here: predates this test, not re-measured now
    ),
    (
        "icons.svg",
        "427dade0e33c0a3a7fe50e4136c23a737295077541396e0bed2572e2f87cc186",
        7684,
    ),
    (
        "fonts/montserrat-latin-var.woff2",
        "06b16db7a969135d48d38c49183be7fb88d4452e2a3011957c7851941f4e4879",
        37956,
    ),
]


def test_vendored_assets_match_their_pinned_hash():
    for rel_path, expected_sha256, expected_size in _PINNED_ASSETS:
        data = (_STATIC_DIR / rel_path).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        assert actual == expected_sha256, (
            f"{rel_path} changed (sha256 {actual}); update the pin deliberately "
            "if this was an intentional vendor update"
        )
        if expected_size is not None:
            assert len(data) == expected_size, (
                f"{rel_path} is {len(data)} bytes, expected {expected_size}"
            )
