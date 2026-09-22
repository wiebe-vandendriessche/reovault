from pathlib import Path

from reovault.web import assets
from reovault.web.assets import compute_asset_version

_STATIC_DIR = Path(__file__).resolve().parents[2] / "reovault" / "web" / "static"


def _seed(tmp_path: Path) -> None:
    """One placeholder file per `_ASSET_FILES` entry (some nested under
    fonts/icons, see assets.py), so this test tracks whatever the module
    actually hashes instead of a copy of an old, shorter list."""
    for name in assets._ASSET_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")


def test_asset_version_is_stable_and_deterministic():
    v1 = compute_asset_version(_STATIC_DIR)
    v2 = compute_asset_version(_STATIC_DIR)
    assert v1 == v2
    assert len(v1) == 8


def test_asset_version_changes_when_an_asset_changes(tmp_path):
    _seed(tmp_path)
    before = compute_asset_version(tmp_path)
    (tmp_path / "app.css").write_bytes(b"body{color:red}")
    after = compute_asset_version(tmp_path)
    assert before != after


def test_asset_version_changes_when_the_wordmark_changes(tmp_path):
    """The PWA-icon caching bug this guards against: a branding-only change
    must rename the service worker's cache too, not just the CSS/JS
    bundle."""
    _seed(tmp_path)
    before = compute_asset_version(tmp_path)
    (tmp_path / "icons" / "wordmark-light.png").write_bytes(b"new-wordmark-bytes")
    after = compute_asset_version(tmp_path)
    assert before != after
