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
alias = "demo"
timezone = "Europe/Brussels"
demo = true

[backup]
repository = "{tmp_path / "repos"}"
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
        assert backed_up > 600  # a week of clips every 15 minutes, minus the lost ones
        assert run.lost >= 1  # every 97th demo clip is overwritten on download
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
    snapshot = next(iter(restic.snapshot_ids()))
    subprocess.run(
        [restic.binary, "-r", restic.repository, "forget", snapshot, "--prune"],
        env=restic.env,
        check=True,
        capture_output=True,
    )
    worker._reconcile(restic)
    expired = db._all("SELECT COUNT(*) AS n FROM clips WHERE state = 'expired'")[0]["n"]
    assert expired > 0


def test_an_empty_answer_never_expires_the_whole_index(setup, tmp_path):
    store, db = setup
    worker = _backed_up_once(store, db)
    before = db.totals("demo")
    other = restic_for(store.current, "other")
    other.init()  # a valid, empty repository answering instead
    worker._reconcile(other)
    assert worker.health.index_mismatch is True
    assert db.totals("demo") == before
