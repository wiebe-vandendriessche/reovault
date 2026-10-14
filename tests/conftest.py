import os

import pytest


@pytest.fixture(autouse=True)
def isolated_settings_sources(tmp_path_factory, monkeypatch):
    """`Settings()` reads `$REOVAULT_CONFIG_FILE` (default `./reovault.toml`)
    and every `REOVAULT_*` env var. A developer's real config file in the
    repo root, or a variable exported in their shell, must never reach a
    test: point the file at one that doesn't exist and clear the env. Tests
    that need either set it themselves, after this runs."""
    for key in [k for k in os.environ if k.startswith("REOVAULT_")]:
        monkeypatch.delenv(key)
    missing = tmp_path_factory.getbasetemp() / "no-reovault.toml"
    monkeypatch.setenv("REOVAULT_CONFIG_FILE", str(missing))
