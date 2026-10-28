"""The backup engine against a fake camera and the real restic binary."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reovault import backup
from reovault.backup import BackupRun, CameraState
from reovault.db import Db
from reovault.restic import Restic
from reovault.tools import ToolError
from tests.fake_camera import FakeCamera

INTERVAL = timedelta(hours=1)
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def restic_bin() -> str:
    found = shutil.which("restic")
    if found is None:
        pytest.fail("restic is not on PATH; install the pinned version (see CONTRIBUTING.md)")
    return found


@pytest.fixture
def env(tmp_path, restic_bin):
    pw = tmp_path / "restic_password"
    pw.write_text("test-password")
    restic_env = {"RESTIC_PASSWORD_FILE": str(pw), "RESTIC_CACHE_DIR": str(tmp_path / "cache")}

    class Env:
        cam = FakeCamera()
        db = Db(tmp_path / "reovault.db")
        repo_base = tmp_path / "repos"
        restic = Restic.for_camera(restic_bin, str(tmp_path / "repos"), "cam", restic_env)
        staging = tmp_path / "staging" / "cam"
        state = CameraState()

        def run(self, trigger: str = "schedule") -> backup.RunResult:
            return BackupRun(
                camera=self.cam,
                restic=self.restic,
                db=self.db,
                staging=self.staging,
                state=self.state,
                interval=INTERVAL,
                sleep=lambda _s: None,
            ).run(trigger)

        def states(self) -> dict[str, str]:
            rows = self.db._all("SELECT name, state FROM clips")
            return {r["name"]: r["state"] for r in rows}

        def snapshots(self) -> set[str]:
            return self.restic.snapshot_ids()

    yield Env()


def _staged_files(staging: Path) -> list[Path]:
    return [p for p in staging.rglob("*") if p.is_file() and p.name != ".lock"]


def test_a_run_backs_up_the_card_and_restic_returns_the_same_bytes(env):
    names = [env.cam.add(NOW - timedelta(hours=h)) for h in (30, 5, 1)]
    result = env.run()
    assert result.outcome == "ok", result.error
    assert result.clips == 3
    assert set(env.states().values()) == {"backed_up"}
    assert _staged_files(env.staging) == []
    clip = env.db.clips_by_name("cam", [names[0]])[names[0]]
    proc = env.restic.dump(clip.snapshot_id, clip.path)
    data, _ = proc.communicate()
    assert data == env.cam.card[names[0]][1]
    assert clip.path.startswith(str(env.staging / "batch"))


def test_nothing_is_downloaded_twice(env):
    env.cam.add(NOW - timedelta(hours=3))
    env.run()
    env.cam.downloads.clear()
    assert env.run().clips == 0
    assert env.cam.downloads == []


def test_new_clips_go_first_then_the_oldest(env, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_BYTES", 2000)  # two 1000-byte clips per batch
    old = [env.cam.add(NOW - timedelta(days=d)) for d in (3, 2, 1)]
    new = [env.cam.add(NOW - timedelta(minutes=m)) for m in (20, 10)]
    seen_sizes = []
    env.cam.on_download = lambda dest: seen_sizes.append(
        sum(p.stat().st_size for p in _staged_files(env.staging))
    )
    assert env.run().clips == 5
    assert env.cam.downloads[0] == sorted(new)
    assert env.cam.downloads[1] == old[:2]  # oldest first
    assert env.cam.downloads[2] == old[2:]
    assert max(seen_sizes) == 0  # never more than one batch on disk at once
    assert len(env.snapshots()) == 3


def test_an_offline_camera_blames_no_clip(env):
    name = env.cam.add(NOW - timedelta(hours=2))
    env.run()  # backs it up
    pending = env.cam.add(NOW - timedelta(minutes=5))
    env.cam.offline = True
    result = env.run()
    assert result.outcome == "error"
    assert "unreachable" in result.error
    env.cam.offline = False
    clips = env.db.clips_by_name("cam", [name, pending])
    assert pending not in clips or clips[pending].attempts == 0


def test_a_short_download_is_retried_once(env):
    name = env.cam.add(NOW - timedelta(hours=2))
    env.cam.short_once.add(name)
    assert env.run().clips == 1
    assert env.states()[name] == "backed_up"


def test_a_clip_that_stays_short_counts_an_attempt_and_stays_pending(env):
    name = env.cam.add(NOW - timedelta(hours=2))
    env.cam.always_short.add(name)
    env.run()
    clip = env.db.clips_by_name("cam", [name])[name]
    assert clip.state == "pending"
    assert clip.attempts == 1
    assert "expected 1000" in clip.last_error


def test_a_clip_the_card_overwrote_is_lost(env):
    name = env.cam.add(NOW - timedelta(days=2))
    env.cam.vanish.add(name)
    result = env.run()
    assert env.states()[name] == "lost"
    assert result.lost == 1


def test_a_refused_clip_still_on_the_card_is_retried_not_lost(env):
    name = env.cam.add(NOW - timedelta(days=2))
    env.cam.refuse.add(name)
    env.run()
    clip = env.db.clips_by_name("cam", [name])[name]
    assert (clip.state, clip.attempts) == ("pending", 1)


def test_a_failed_upload_keeps_the_batch_and_the_next_run_sends_it(env, monkeypatch):
    names = [env.cam.add(NOW - timedelta(hours=h)) for h in (3, 2)]
    real = env.restic.backup

    def broken(path, timeout):
        raise ToolError("restic backup failed: connection refused")

    monkeypatch.setattr(type(env.restic), "backup", lambda self, p, timeout: broken(p, timeout))
    result = env.run()
    assert result.outcome == "error"
    assert len(_staged_files(env.staging / "batch")) == 2  # kept, not re-downloaded later
    monkeypatch.setattr(type(env.restic), "backup", lambda self, p, timeout: real(p, timeout))
    env.cam.downloads.clear()
    assert env.run().outcome == "ok"
    assert env.cam.downloads == []  # recovered from the batch folder
    assert all(env.states()[n] == "backed_up" for n in names)
    assert _staged_files(env.staging) == []


def test_a_crash_after_restic_but_before_the_commit_counts_once(env, monkeypatch):
    name = env.cam.add(NOW - timedelta(hours=2))
    real = env.db.mark_backed_up
    calls = []

    def crash_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(env.db, "mark_backed_up", crash_once)
    assert env.run().outcome == "error"
    assert env.states()[name] == "pending"
    assert env.run().outcome == "ok"
    assert env.states()[name] == "backed_up"
    assert len(env.snapshots()) == 2  # one extra snapshot, same data


def test_a_vanished_repository_with_history_is_never_recreated(env):
    env.cam.add(NOW - timedelta(hours=2))
    env.run()
    shutil.rmtree(env.repo_base)  # e.g. the backup disk is no longer mounted
    env.cam.add(NOW - timedelta(minutes=5))
    result = env.run()
    assert result.outcome == "error"
    assert "nothing will be created" in result.error
    assert not env.repo_base.exists()


def test_a_wrong_password_stops_the_run(env, tmp_path):
    env.cam.add(NOW - timedelta(hours=2))
    env.run()
    (tmp_path / "restic_password").write_text("not-it")
    result = env.run()
    assert result.outcome == "error"
    assert "password" in result.error


def test_it_works_through_an_rclone_remote(tmp_path, restic_bin):
    rclone = shutil.which("rclone")
    if rclone is None:
        pytest.fail("rclone is not on PATH; install the pinned version (see CONTRIBUTING.md)")
    conf = tmp_path / "rclone.conf"
    conf.write_text("[loc]\ntype = local\n")
    pw = tmp_path / "pw"
    pw.write_text("x")
    restic = Restic.for_camera(
        restic_bin,
        f"rclone:loc:{tmp_path / 'remote'}",
        "cam",
        {"RESTIC_PASSWORD_FILE": str(pw), "RCLONE_CONFIG": str(conf)},
    )
    cam = FakeCamera()
    name = cam.add(NOW - timedelta(hours=1))
    db = Db(tmp_path / "db.sqlite")
    run = BackupRun(cam, restic, db, tmp_path / "staging" / "cam", CameraState(), INTERVAL)
    assert run.run("manual").outcome == "ok"
    assert db.clips_by_name("cam", [name])[name].state == "backed_up"
    assert (tmp_path / "remote" / "cam" / "config").exists()
    subprocess.run([rclone, "version"], check=True, capture_output=True)
