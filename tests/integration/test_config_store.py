"""reovault.toml is the source of truth both ways: dashboard writes keep the
file's comments, hand edits apply live, and infrastructure waits for a restart."""

import threading
import time
import tomllib

import pytest

from reovault.config import load_settings
from reovault.config_store import (
    ConfigConflictError,
    ConfigInvalidError,
    ConfigStore,
    set_fields,
    table_at,
    upsert_camera_entry,
)

COMMENTED = """# My ReoVault config. Keep this comment.

[[cameras]]
alias = "doorbell"  # the front door
timezone = "Europe/Brussels"

# Keep footage for three months.
[backup]
keep_days = 90
"""


@pytest.fixture
def toml_path(tmp_path):
    path = tmp_path / "reovault.toml"
    path.write_text(COMMENTED)
    return path


@pytest.fixture
def store(toml_path):
    return ConfigStore(toml_path, load_settings(toml_path))


def _keep(days):
    return lambda doc: set_fields(table_at(doc, "backup"), {"keep_days": days})


def test_update_keeps_comments_and_layout(store, toml_path):
    store.update(_keep(30))
    text = toml_path.read_text()
    assert "# My ReoVault config. Keep this comment." in text
    assert 'alias = "doorbell"  # the front door' in text
    assert "# Keep footage for three months." in text
    assert "keep_days = 30" in text
    assert store.current.backup.keep_days == 30
    assert (toml_path.parent / "reovault.toml.bak").read_text() == COMMENTED


def test_an_invalid_update_never_touches_the_file(store, toml_path):
    with pytest.raises(ConfigInvalidError, match="keep_days"):
        store.update(_keep(-1))
    assert toml_path.read_text() == COMMENTED
    assert store.current.backup.keep_days == 90


def test_a_stale_version_is_refused(store, toml_path):
    version = store.version
    toml_path.write_text(COMMENTED + "\n[web]\nsession_max_age_days = 7\n")
    with pytest.raises(ConfigConflictError):
        store.update(lambda doc: None, expected_version=version)


def test_a_hand_edit_is_applied_and_announced(store, toml_path):
    seen = []
    store.subscribe(lambda old, new: seen.append(new.backup.keep_days))
    toml_path.write_text(COMMENTED.replace("keep_days = 90", "keep_days = 14"))
    assert store.reload() is True
    assert store.current.backup.keep_days == 14
    assert seen == [14]
    assert store.last_origin == "file"
    store.update(_keep(21))
    assert store.last_origin == "dashboard"


def test_our_own_write_is_not_applied_twice(store):
    seen = []
    store.subscribe(lambda old, new: seen.append(1))
    store.update(_keep(30))
    assert store.reload() is True  # the watcher firing for our own write
    assert seen == [1]


def test_a_broken_hand_edit_keeps_the_last_valid_settings(store, toml_path):
    store.update(_keep(30))
    toml_path.write_text("[backup\nkeep_days = 5\n")
    assert store.reload() is False
    assert "Not valid TOML" in store.error
    assert store.current.backup.keep_days == 30
    toml_path.write_text(COMMENTED)
    assert store.reload() is True
    assert store.error is None


def test_infrastructure_changes_wait_for_a_restart(store, toml_path):
    startup_port = store.current.web.port
    toml_path.write_text(
        COMMENTED
        + "\n[web]\nport = 9999\nsession_max_age_days = 7\n"
        + '\n[tools]\nrestic = "/opt/restic"\n'
    )
    store.reload()
    assert store.current.web.port == startup_port
    assert store.current.tools.restic == "restic"
    assert store.current.web.session_max_age_days == 7  # safe field: live
    assert store.restart_required == ["tools", "web.port"]


def test_adding_a_camera_appends_an_entry(store, toml_path):
    store.update(lambda doc: upsert_camera_entry(doc, "driveway", timezone="UTC", name="Drive"))
    data = tomllib.loads(toml_path.read_text())
    assert [c["alias"] for c in data["cameras"]] == ["doorbell", "driveway"]
    assert data["cameras"][1] == {"alias": "driveway", "timezone": "UTC", "name": "Drive"}
    assert "enabled" not in data["cameras"][1]  # default left out
    store.update(lambda doc: upsert_camera_entry(doc, "driveway", enabled=False))
    assert store.current.camera("driveway").enabled is False


def test_the_watcher_picks_up_an_edit(store, toml_path):
    stop = threading.Event()
    store.watch(stop)
    try:
        time.sleep(0.3)
        toml_path.write_text(COMMENTED.replace("keep_days = 90", "keep_days = 7"))
        deadline = time.monotonic() + 10
        while store.current.backup.keep_days != 7:
            assert time.monotonic() < deadline, "watcher did not reload"
            time.sleep(0.1)
    finally:
        stop.set()
