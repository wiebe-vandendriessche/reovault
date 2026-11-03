"""The backup engine against a fake camera and the real restic binary."""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
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
        commits = 0
        hold = None

        def run(self, trigger: str = "schedule") -> backup.RunResult:
            return BackupRun(
                camera=self.cam,
                restic=self.restic,
                db=self.db,
                staging=self.staging,
                state=self.state,
                interval=INTERVAL,
                sleep=lambda _s: None,
                on_commit=self._committed,
                hold=lambda: self.hold,
            ).run(trigger)

        def _committed(self) -> None:
            self.commits += 1

        def states(self) -> dict[str, str]:
            rows = self.db._all("SELECT name, state FROM clips")
            return {r["name"]: r["state"] for r in rows}

        def snapshots(self) -> set[str]:
            return {s.id for s in self.restic.snapshots()}

    yield Env()


def _staged_files(staging: Path) -> list[Path]:
    """Clips on local disk (not the lock, not the manifest)."""
    return [p for p in staging.rglob("*.mp4") if p.is_file()]


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


def test_every_committed_batch_tells_the_copies(env):
    env.cam.add(NOW - timedelta(hours=3))
    env.run()
    assert env.commits == 1


def test_a_hold_stops_the_run_before_any_download(env):
    env.cam.add(NOW - timedelta(hours=3))
    env.hold = "waiting for the copies"
    result = env.run()
    assert (result.outcome, result.error) == ("stopped", "waiting for the copies")
    assert env.cam.downloads == [] and env.commits == 0


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
    # While a batch downloads, at most the previous one waits in batch/.
    assert max(seen_sizes) <= 2000
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
    env.cam.add(NOW - timedelta(days=2) + timedelta(hours=1))  # the card still has newer clips
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
    assert len(env.snapshots()) == 1  # the rebuild adopted the snapshot the crash orphaned


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


def test_a_vanished_clip_without_proof_is_retried_not_lost(env):
    """An empty or truncated listing proves nothing: only a clip older than
    the oldest one the card still lists is lost."""
    name = env.cam.add(NOW - timedelta(days=2))
    env.cam.vanish.add(name)  # and nothing newer on the card that day
    env.run()
    clip = env.db.clips_by_name("cam", [name])[name]
    assert (clip.state, clip.attempts) == ("pending", 1)


def test_one_bad_clip_does_not_block_its_batch(env):
    good = [env.cam.add(NOW - timedelta(hours=h)) for h in (5, 4)]
    bad = env.cam.add(NOW - timedelta(hours=3))
    env.cam.breaks_batch.add(bad)
    result = env.run()
    assert result.outcome == "ok", result.error
    assert all(env.states()[g] == "backed_up" for g in good)
    clip = env.db.clips_by_name("cam", [bad])[bad]
    assert (clip.state, clip.attempts) == ("pending", 1)


def test_a_clip_without_a_size_waits(env):
    name = env.cam.add(NOW - timedelta(hours=2))
    found, data = env.cam.card[name]
    env.cam.card[name] = (dataclasses.replace(found, size=None), data)
    assert env.run().clips == 0
    assert env.cam.downloads == []
    assert env.states()[name] == "pending"


def test_a_run_that_finds_another_in_progress_leaves_no_row(env):
    from reovault.backup import camera_lock

    env.cam.add(NOW - timedelta(hours=2))
    with camera_lock(env.staging):  # e.g. `reovault run` next to the daemon
        result = env.run()
    assert result.outcome == "skipped"
    assert env.db.runs("cam") == []


def test_an_unexpected_error_ends_the_run_not_the_thread(env, monkeypatch):
    env.cam.add(NOW - timedelta(hours=2))
    monkeypatch.setattr(type(env.cam), "storage", lambda self: 1 / 0)
    monkeypatch.setattr(
        backup, "scan_card", lambda cam, now: (_ for _ in ()).throw(ValueError("bad time"))
    )
    result = env.run()
    assert (result.outcome, result.error) == ("error", "unexpected error: bad time")
    (run,) = env.db.runs("cam")
    assert run.outcome == "error" and run.finished_at


def test_every_snapshot_carries_the_whole_index(env):
    import json as _json

    first = env.cam.add(NOW - timedelta(hours=3), rec_type="md,people")
    env.run()
    second = env.cam.add(NOW - timedelta(minutes=5), rec_type="vehicle")
    env.run()
    latest = env.restic.latest()
    data = _json.loads(env.restic.read(latest.id, f"{latest.paths[0]}/.reovault/index.json"))
    by_name = {c["name"]: c for c in data["clips"]}
    assert set(by_name) == {first, second}
    db_first = env.db.clips_by_name("cam", [first])[first]
    assert by_name[first]["start_utc"] == db_first.start_utc  # verbatim, so rebuilt rows match
    assert by_name[first]["rec_type"] == "md,people"
    assert by_name[second]["snapshot_id"] is None  # "this snapshot"
    assert not (env.staging / "batch" / ".reovault").exists()  # cleaned up after the commit


def test_a_lost_database_is_rebuilt_without_downloading_anything(env, tmp_path):
    gone = env.cam.add(NOW - timedelta(hours=40))
    env.cam.vanish.add(gone)  # overwritten on the card before it could be backed up
    names = [env.cam.add(NOW - timedelta(hours=h), rec_type="md,vehicle") for h in (30, 5, 1)]
    env.run()
    assert env.states()[gone] == "lost"
    env.cam.add(NOW - timedelta(minutes=5))
    env.run()  # a later snapshot, whose index includes the lost clip
    before = {
        c.name: (c.state, c.rec_type, c.snapshot_id, c.path)
        for c in env.db.clips_between("cam", NOW - timedelta(days=3), NOW + timedelta(hours=3))
    }
    env.db = Db(tmp_path / "fresh.db")  # the old database is gone
    env.cam.downloads.clear()
    assert env.run().outcome == "ok"
    assert env.cam.downloads == []
    after = {
        c.name: (c.state, c.rec_type, c.snapshot_id, c.path)
        for c in env.db.clips_between("cam", NOW - timedelta(days=3), NOW + timedelta(hours=3))
    }
    assert after == before
    assert set(names) | {gone} <= set(after)
    assert after[gone][0] == "lost"  # lost clips come back too, not only backed-up ones


def test_a_partial_database_catches_up(env, tmp_path):
    env.cam.add(NOW - timedelta(hours=5))
    env.run()
    with sqlite3.connect(tmp_path / "old.db") as old:  # a backup of the DB after run 1
        env.db.conn.backup(old)
    env.cam.add(NOW - timedelta(minutes=10))
    env.run()
    env.db = Db(tmp_path / "old.db")
    env.cam.downloads.clear()
    env.run()
    assert env.cam.downloads == []
    assert set(env.states().values()) == {"backed_up"}


def test_snapshots_without_an_index_fail_loudly(env, tmp_path):
    env.cam.add(NOW - timedelta(hours=2))
    env.run()
    # A snapshot made without ReoVault's manifest, e.g. by hand.
    other = tmp_path / "manual"
    other.mkdir()
    (other / "note.txt").write_text("x")
    subprocess.run(
        [
            env.restic.binary,
            "-r",
            env.restic.repository,
            "backup",
            "--host",
            "reovault",
            str(other),
        ],
        env=env.restic.env,
        check=True,
        capture_output=True,
    )
    env.db = Db(tmp_path / "fresh.db")
    result = env.run()
    assert result.outcome == "error"
    assert "no readable index" in result.error


def test_an_upload_failing_mid_run_keeps_both_batches(env, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_BYTES", 2000)
    names = [env.cam.add(NOW - timedelta(days=d)) for d in (4, 3, 2, 1)]
    real = env.restic.backup
    calls = []

    def second_fails(self, path, timeout):
        calls.append(1)
        if len(calls) == 2:
            raise ToolError("restic backup failed: connection reset")
        return real(path, timeout)

    monkeypatch.setattr(type(env.restic), "backup", second_fails)
    result = env.run()
    assert result.outcome == "error"
    assert result.clips == 2  # the first batch made it, and the run row says so
    assert env.db.runs("cam")[0].clips == 2
    monkeypatch.setattr(type(env.restic), "backup", lambda self, p, timeout: real(p, timeout))
    env.cam.downloads.clear()
    assert env.run().outcome == "ok"
    assert all(env.states()[n] == "backed_up" for n in names)
    assert _staged_files(env.staging) == []


def test_stop_during_an_upload_waits_for_it_and_counts_it(env, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_BYTES", 2000)
    for d in (4, 3, 2, 1):
        env.cam.add(NOW - timedelta(days=d))
    real = env.restic.backup

    def stop_now(self, path, timeout):
        env.state.stop.set()  # pressed while the first batch uploads
        return real(path, timeout)

    monkeypatch.setattr(type(env.restic), "backup", stop_now)
    result = env.run()
    assert result.outcome == "stopped"
    assert result.clips >= 2
    assert env.db.runs("cam")[0].clips == result.clips
    assert len(env.cam.downloads) <= 2  # nothing new started after Stop


def test_an_incomplete_snapshot_is_never_trusted_by_a_rebuild(env, monkeypatch):
    """restic exits 3 and still writes a snapshot when a file can't be read;
    the rebuild must not mark the missing clip backed up."""
    good = env.cam.add(NOW - timedelta(hours=3))
    bad = env.cam.add(NOW - timedelta(hours=2))
    real = env.restic.backup

    def unreadable(self, path, timeout):
        (next(path.rglob(f"{bad}.mp4"))).chmod(0)
        return real(path, timeout)

    monkeypatch.setattr(type(env.restic), "backup", unreadable)
    assert env.run().outcome == "error"  # exit 3
    assert len(env.snapshots()) == 1  # but restic did write a snapshot
    monkeypatch.setattr(type(env.restic), "backup", lambda self, p, timeout: real(p, timeout))
    for f in (env.staging / "batch").rglob(f"{bad}.mp4"):
        f.chmod(0o600)
    env.run()
    assert env.states()[good] == "backed_up"
    assert env.states()[bad] == "backed_up"  # only after a real, complete backup
    clip = env.db.clips_by_name("cam", [bad])[bad]
    data, _ = env.restic.dump(clip.snapshot_id, clip.path).communicate()
    assert data == env.cam.card[bad][1]


def test_a_short_file_retry_that_falls_back_keeps_the_good_files(env):
    good = env.cam.add(NOW - timedelta(hours=3))
    short = env.cam.add(NOW - timedelta(hours=2))
    env.cam.short_once.add(short)
    real = env.cam.download

    def retry_breaks(names, dest, timeout):
        if names == [short] and len(env.cam.downloads) == 1:
            env.cam.downloads.append(list(names))
            raise ToolError("unexpected EOF", code=4)  # the retry fails as a whole
        return real(names, dest, timeout)

    env.cam.download = retry_breaks
    assert env.run().outcome == "ok"
    assert env.states()[good] == "backed_up"


def test_an_unreachable_camera_is_marked_and_cleared_without_blaming_a_clip(env):
    from reovault.status import camera_status

    name = env.cam.add(NOW - timedelta(hours=2))
    env.cam.offline = True
    result = env.run()
    assert result.outcome == "error"
    assert env.state.unreachable_since is not None
    status = camera_status("cam", env.db, env.state)
    assert status.verdict == "offline" and "hasn't answered" in status.text
    assert env.db.pending("cam") == []  # never even discovered: nothing is blamed
    env.cam.offline = False
    assert env.run().outcome == "ok"
    assert env.state.unreachable_since is None
    assert env.states()[name] == "backed_up"


def test_a_device_error_is_a_failure_not_an_outage(env):
    env.cam.add(NOW - timedelta(hours=2))
    bad = env.cam.add(NOW - timedelta(hours=1))
    env.cam.breaks_batch.add(bad)
    env.run()
    assert env.state.unreachable_since is None


def test_the_card_timeline_splits_the_card_into_slices(env):
    from reovault.status import BUCKETS, timeline

    for h in (70, 60, 40, 30, 20, 10):
        env.cam.add(NOW - timedelta(hours=h))
    env.cam.refuse.add(env.cam.add(NOW - timedelta(hours=5)))  # keeps failing
    env.run()
    slices = timeline("cam", env.db, env.state)
    assert len(slices) == BUCKETS
    assert sum(b.backed_up for b in slices) == 6
    assert sum(b.waiting + b.failing for b in slices) == 1
    assert slices[0].backed_up == 1  # the oldest clip is the first slice
    assert timeline("cam", env.db, CameraState()) == []  # card not scanned yet: nothing to draw
