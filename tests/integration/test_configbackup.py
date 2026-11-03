"""ReoVault's own config backed up next to the footage, and a full recovery from it."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reovault import backup
from reovault.backup import BackupRun, CameraState
from reovault.config import load_settings
from reovault.configbackup import REPO, ConfigBackup
from reovault.db import Db
from reovault.destinations import DestinationWorker, restic_for, status_of
from tests.fake_camera import FakeCamera

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def site(tmp_path, monkeypatch):
    restic = shutil.which("restic")
    if restic is None:
        pytest.fail("restic is not on PATH (python3 scripts/fetch_tools.py .tools)")
    monkeypatch.setattr(backup, "BATCH_BYTES", 1000)
    (tmp_path / "pw").write_text("test-password")
    conf = tmp_path / "config"
    conf.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    (conf / "rclone.conf").write_text("[nextcloud]\ntype = webdav\npass = OBSCURED\n")
    (registry / "aliases.toml").write_text('[doorbell]\nhost = "10.0.0.5"\n')
    (registry / "credentials.key").write_text("KEYBYTES")
    toml = conf / "reovault.toml"
    toml.write_text(
        f"""
[[cameras]]
alias = "cam"
timezone = "Europe/Brussels"

[[destinations]]
name = "local"
repository = "{tmp_path / "repo1"}"
primary = true

[[destinations]]
name = "second"
repository = "{tmp_path / "repo2"}"

[backup]
password_file = "{tmp_path / "pw"}"

[storage]
data_dir = "{tmp_path / "data"}"
staging_dir = "{tmp_path / "staging"}"

[tools]
restic = "{restic}"
"""
    )
    files = {
        "reovault.toml": toml,
        "rclone.conf": conf / "rclone.conf",
        "reolink-cli/aliases.toml": registry / "aliases.toml",
        "reolink-cli/credentials.key": registry / "credentials.key",
    }
    return tmp_path, load_settings(toml), files


def test_the_config_is_backed_up_to_every_destination_and_only_when_it_changes(site):
    tmp, settings, files = site
    db = Db(tmp / "db.sqlite")
    cb = ConfigBackup(lambda: settings, db, lambda: files)
    cb.run_once()
    for dest in settings.destinations:
        repo = restic_for(settings, REPO, dest)
        (snap,) = repo.snapshots()
        listed = repo.files(snap.id)
        assert any(p.endswith("/reolink-cli/credentials.key") for p in listed)
        assert any(p.endswith("/rclone.conf") for p in listed)
        assert (
            status_of(db, dest.name)["config_ok"]
            and status_of(db, dest.name)["config_error"] is None
        )
    assert not (tmp / "data" / "config-backup").exists()  # the staged plaintext is gone
    cb.run_once()  # nothing changed: nothing backed up again
    assert len(restic_for(settings, REPO, settings.destinations[0]).snapshots()) == 1
    files["rclone.conf"].write_text("[nextcloud]\ntype = webdav\npass = NEWPASS\n")
    cb.run_once()
    assert len(restic_for(settings, REPO, settings.destinations[0]).snapshots()) == 2


def test_an_unreachable_destination_doesnt_stop_the_others(site):
    tmp, settings, files = site
    (tmp / "repo2").write_text("a file where the repository folder should be")
    db = Db(tmp / "db.sqlite")
    ConfigBackup(lambda: settings, db, lambda: files).run_once()
    assert status_of(db, "second")["config_error"]
    assert status_of(db, "local")["config_ok"]
    assert len(restic_for(settings, REPO, settings.destinations[0]).snapshots()) == 1


def test_a_dropped_destination_is_never_recreated_for_the_config_either(site):
    tmp, settings, files = site
    db = Db(tmp / "db.sqlite")
    cb = ConfigBackup(lambda: settings, db, lambda: files)
    cb.run_once()
    shutil.rmtree(tmp / "repo2")
    files["rclone.conf"].write_text("changed")
    cb.run_once()
    assert not (tmp / "repo2").exists()
    assert "nothing will be created" in status_of(db, "second")["config_error"]


def test_a_missing_file_is_skipped_not_an_error(site):
    tmp, settings, files = site
    files["reolink-cli/credentials.key"].unlink()  # e.g. only demo cameras
    db = Db(tmp / "db.sqlite")
    ConfigBackup(lambda: settings, db, lambda: files).run_once()
    assert status_of(db, "local")["config_ok"]


def test_a_lost_server_is_recovered_from_a_copy_destination(site):
    """The whole story: the server, its database and its config are gone. Only
    the destinations and the restic password survive."""
    tmp, settings, files = site
    cam = FakeCamera()
    names = [cam.add(NOW - timedelta(days=d), rec_type="md,people") for d in (3, 2, 1)]
    db = Db(tmp / "db.sqlite")
    primary = settings.primary()

    def run(db_: Db, settings_) -> backup.RunResult:
        return BackupRun(
            camera=cam,
            restic=restic_for(settings_, "cam", settings_.primary()),
            db=db_,
            staging=tmp / "staging" / "cam",
            state=CameraState(),
            interval=timedelta(hours=1),
            destination=settings_.primary().name,
        ).run("manual")

    assert run(db, settings).outcome == "ok"
    DestinationWorker("second", lambda: settings, db, lambda *a, **k: None).run_round()
    ConfigBackup(lambda: settings, db, lambda: files).run_once()
    original = {n: p.read_text() for n, p in files.items()}

    # --- disaster: the primary's disk, the database and every config file are gone ---
    shutil.rmtree(tmp / "repo1")
    shutil.rmtree(tmp / "data")
    shutil.rmtree(tmp / "staging")
    for p in files.values():
        p.unlink()
    del db

    # --- recovery: plain restic, from the copy, with only the password ---
    restored = tmp / "restored"
    repo2 = f"{tmp / 'repo2'}/{REPO}"
    proc = subprocess.run(
        [settings.tools.restic, "-r", repo2, "--no-cache", "restore", "latest", "--target",
         str(restored)],
        env={"RESTIC_PASSWORD_FILE": str(tmp / "pw"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    found = {
        str(p.relative_to(next(restored.rglob("config-backup")))): p.read_text()
        for p in restored.rglob("*")
        if p.is_file()
    }
    assert found == original  # the config, the rclone remotes and the camera registry, intact

    # --- the new server starts from the restored config: promote the surviving copy ---
    new_toml = Path(tmp / "config" / "reovault.toml")
    new_toml.parent.mkdir(exist_ok=True)
    new_toml.write_text(
        found["reovault.toml"]
        .replace("primary = true\n", "")
        .replace('name = "second"\nrepository', 'name = "second"\nprimary = true\nrepository')
        .replace(f'[[destinations]]\nname = "local"\nrepository = "{tmp / "repo1"}"\n\n', "")
    )
    new_settings = load_settings(new_toml)
    assert new_settings.primary().name == "second"
    fresh = Db(tmp / "fresh.sqlite")
    cam.downloads.clear()
    assert run(fresh, new_settings).outcome == "ok"
    assert cam.downloads == []  # the index came back from the footage: nothing downloaded again
    clips = fresh.clips_by_name("cam", names)
    assert {c.state for c in clips.values()} == {"backed_up"}
    assert {c.rec_type for c in clips.values()} == {"md,people"}  # detection types survived too
    for c in clips.values():
        assert fresh.clip_location(c, ["second"]) is not None
    assert primary.name == "local"
