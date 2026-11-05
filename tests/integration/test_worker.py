"""Workers: a demo camera end to end, and retention's view in the index."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from reovault import backup
from reovault.camera import Gateway
from reovault.config import load_settings
from reovault.config_store import ConfigStore
from reovault.db import Db
from reovault.worker import Worker, Workers, restic_for


@pytest.fixture
def setup(tmp_path: Path):
    if shutil.which("restic") is None:
        pytest.fail("restic is not on PATH; install the pinned version (see CONTRIBUTING.md)")
    pw = tmp_path / "pw"
    pw.write_text("x")
    toml = tmp_path / "reovault.toml"
    toml.write_text(
        f"""
[[cameras]]
id = "demo"
timezone = "Europe/Brussels"
demo = true

[[destinations]]
name = "local"
repository = "{tmp_path / "repos"}"

[backup]
password_file = "{pw}"

[storage]
data_dir = "{tmp_path / "data"}"
staging_dir = "{tmp_path / "staging"}"
"""
    )
    store = ConfigStore(toml, load_settings(toml))
    db = Db(tmp_path / "data" / "reovault.db")
    return store, db


def _wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def test_a_demo_camera_is_backed_up_by_its_worker(setup):
    store, db = setup
    events = []
    workers = Workers(
        lambda: store.current,
        db,
        Gateway("reolink-cli", "127.0.0.1:1"),
        lambda t, **d: events.append(t),
    )
    workers.sync()
    try:
        _wait_for(lambda: workers.get("demo").next_run_at is not None)
        run = db.runs("demo")[0]
        assert run.outcome == "ok", run.error
        backed_up, _ = db.totals("demo")
        assert backed_up > 600  # a week of clips every 15 minutes
        assert "activity" in events
        assert workers.get("demo").next_run_at is not None
    finally:
        workers.shutdown()


def _backed_up_once(store, db) -> Worker:
    worker = Worker(
        "demo",
        lambda: store.current,
        db,
        Gateway("reolink-cli", "127.0.0.1:1"),
        lambda *a, **k: None,
    )
    settings = store.current
    worker._backup(settings, settings.camera("demo"))
    return worker


def test_clips_of_a_forgotten_snapshot_become_expired(setup, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_BYTES", 1024**2)  # several snapshots
    store, db = setup
    worker = _backed_up_once(store, db)
    restic = restic_for(store.current, "demo")
    snapshot = restic.snapshots()[0].id
    subprocess.run(
        [restic.binary, "-r", restic.repository, "forget", snapshot, "--prune"],
        env=restic.env,
        check=True,
        capture_output=True,
    )
    worker._retain(store.current, store.current.primary(), restic)
    expired = db._all("SELECT COUNT(*) AS n FROM clips WHERE state = 'expired'")[0]["n"]
    assert expired > 0


def test_an_empty_answer_never_expires_the_whole_index(setup, tmp_path):
    """A valid but empty repository answering (the wrong or a wiped one) must
    not make ReoVault forget everything it has backed up."""
    store, db = setup
    worker = _backed_up_once(store, db)
    before = db.totals("demo")
    primary = store.current.primary()
    other = restic_for(store.current, "other")
    other.init()
    # The same listing a reconcile would act on, but from the empty repository.
    assert db.set_locations("demo", primary.name, []) is False
    assert db.expire_unlocated("demo") == 0
    assert db.totals("demo") == before
    assert worker is not None


def test_a_changed_camera_entry_is_used_from_the_next_run(setup, monkeypatch):
    """A new password (or address) must not wait for a restart."""
    from reovault import worker as worker_mod

    store, db = setup
    built = []
    monkeypatch.setattr(
        worker_mod, "camera_for", lambda _s, cfg, _g: built.append(cfg.name) or object()
    )
    monkeypatch.setattr(backup.BackupRun, "run", lambda self, trigger: backup.RunResult())
    gateway = Gateway("reolink-cli", "127.0.0.1:1")
    w = Worker("demo", lambda: store.current, db, gateway, lambda *_a, **_k: None)
    cfg = store.current.camera("demo")
    w._backup(store.current, cfg)
    w._backup(store.current, cfg)
    assert built == [cfg.name]  # unchanged: the same camera object is kept
    w._backup(store.current, cfg.model_copy(update={"name": "renamed"}))
    assert built == [cfg.name, "renamed"]
