"""Copy destinations against the real restic: copying, retention that never
strands a clip, quotas, and a lost database across two destinations."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reovault import backup, retention
from reovault.backup import BackupRun, CameraState
from reovault.camera import Gateway
from reovault.config import load_settings
from reovault.db import Db
from reovault.destinations import DestinationWorker, quota_short, restic_for, status_of
from reovault.worker import Worker
from tests.fake_camera import FakeCamera

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    restic = shutil.which("restic")
    if restic is None:
        pytest.fail("restic is not on PATH (python3 scripts/fetch_tools.py .tools)")
    (tmp_path / "pw").write_text("test-password")
    monkeypatch.setattr(backup, "BATCH_BYTES", 1000)  # one 1000-byte clip per snapshot
    monkeypatch.setattr(retention, "GiB", 1000)  # so `max_gb = 2` is a 2000-byte cap

    def configure(primary_extra: str = "", second_extra: str = ""):
        toml = tmp_path / "reovault.toml"
        toml.write_text(
            f"""
[[cameras]]
alias = "cam"
timezone = "Europe/Brussels"

[[destinations]]
name = "local"
repository = "{tmp_path / "repo1"}"
primary = true
{primary_extra}

[[destinations]]
name = "second"
repository = "{tmp_path / "repo2"}"
{second_extra}

[backup]
password_file = "{tmp_path / "pw"}"

[storage]
data_dir = "{tmp_path / "data"}"
staging_dir = "{tmp_path / "staging"}"

[tools]
restic = "{restic}"
"""
        )
        return load_settings(toml)

    class Env(SimpleNamespace):
        def backup(self) -> backup.RunResult:
            primary = self.settings.primary()
            return BackupRun(
                camera=self.cam,
                restic=restic_for(self.settings, "cam", primary),
                db=self.db,
                staging=tmp_path / "staging" / "cam",
                state=self.state,
                interval=timedelta(hours=1),
                destination=primary.name,
            ).run("manual")

        def reconfigure(self, **kw) -> None:
            self.settings = self.configure(**kw)

        def copy_round(self) -> None:
            self.copier.run_round()

        def snaps(self, name: str):
            return restic_for(self.settings, "cam", self.settings.destination(name)).snapshots()

        def worker(self) -> Worker:
            return Worker("cam", lambda: self.settings, self.db, Gateway("x", "127.0.0.1:1"), print)

    def build(**kw):
        settings = configure(**kw)
        e = Env(
            settings=settings,
            db=Db(tmp_path / "db.sqlite"),
            cam=FakeCamera(),
            state=CameraState(),
            tmp=tmp_path,
            configure=configure,
        )
        e.copier = DestinationWorker("second", lambda: e.settings, e.db, lambda *a, **k: None)
        return e

    return build


def _fill(env, n: int = 4) -> list[str]:
    names = [env.cam.add(NOW - timedelta(days=d)) for d in range(n, 0, -1)]
    assert env.backup().outcome == "ok"
    return names


def test_a_copy_destination_receives_every_snapshot_once(env):
    e = env()
    _fill(e)
    e.copy_round()
    primary, second = e.snaps("local"), e.snaps("second")
    assert len(primary) == 4 and len(second) == 4
    assert {s.root for s in second} == {s.root for s in primary}
    assert {s.id for s in second}.isdisjoint({s.id for s in primary})  # copies get new ids
    e.copy_round()  # nothing new: nothing copied again
    assert len(e.snaps("second")) == 4
    assert e.db.location_roots("cam", "second") == {s.root for s in primary}
    assert status_of(e.db, "second")["error"] is None


def test_a_new_backup_is_copied_on_the_next_round(env):
    e = env()
    _fill(e, 2)
    e.copy_round()
    e.cam.add(NOW - timedelta(minutes=5))
    e.backup()
    e.copy_round()
    assert len(e.snaps("second")) == 3


def test_a_clip_is_still_downloadable_after_the_primary_forgot_it(env):
    e = env()
    names = _fill(e, 2)
    e.copy_round()
    oldest = min(e.snaps("local"), key=lambda s: s.time)
    restic = restic_for(e.settings, "cam", e.settings.primary())
    restic.forget_ids([oldest.id])
    e.db.set_locations("cam", "local", [(s.root, s.id, s.time) for s in restic.snapshots()])
    assert e.db.expire_unlocated("cam") == 0  # the copy still holds it
    (clip,) = e.db.clips_by_name("cam", [names[0]]).values()
    assert clip.state == "backed_up"
    dest, snapshot_id = e.db.clip_location(clip, ["local", "second"])
    assert dest == "second"
    data, _ = (
        restic_for(e.settings, "cam", e.settings.destination("second"))
        .dump(snapshot_id, clip.path)
        .communicate()
    )
    assert data == e.cam.card[names[0]][1]


def test_an_unreachable_copy_never_blocks_a_backup(env):
    e = env()
    _fill(e, 1)
    (e.tmp / "repo2").write_text("a file where the repository folder should be")
    e.copy_round()
    status = status_of(e.db, "second")
    assert status["error"] and status["failures"] == 1
    e.cam.add(NOW - timedelta(minutes=5))
    assert e.backup().outcome == "ok"  # backups don't depend on the copy at all


def test_a_dropped_copy_mount_is_never_recreated(env):
    e = env()
    _fill(e, 1)
    e.copy_round()
    shutil.rmtree(e.tmp / "repo2")  # the disk is no longer mounted
    e.copy_round()
    assert not (e.tmp / "repo2").exists()
    assert "nothing will be created" in status_of(e.db, "second")["error"]


def test_the_primary_keeps_every_snapshot_until_the_copy_has_it(env):
    """A quota on the primary must never forget footage that exists nowhere else."""
    e = env(primary_extra="max_gb = 2")  # a 2000-byte cap, 4000 bytes held
    _fill(e, 4)
    w, primary = e.worker(), e.settings.primary()
    restic = restic_for(e.settings, "cam", primary)
    w._retain(e.settings, primary, restic)
    assert len(restic.snapshots()) == 4  # the copy has none of them: nothing may go
    assert status_of(e.db, "local")["blocked"] >= 1

    e.copy_round()
    w._retain(e.settings, primary, restic)
    assert len(restic.snapshots()) == 1  # copied, so the quota may trim the primary now
    assert len(e.snaps("second")) == 4
    states = {c.state for c in e.db.clips_between("cam", NOW - timedelta(days=9), NOW)}
    assert states == {"backed_up"}  # every clip still has a home


def test_a_copy_with_a_small_quota_stops_before_copying_what_it_would_forget(env):
    e = env(second_extra="max_gb = 2")  # a 2000-byte cap: one 1000-byte snapshot, not two
    _fill(e, 4)
    e.copy_round()
    held = e.snaps("second")
    assert len(held) == 1
    assert held[0].root == max(e.snaps("local"), key=lambda s: s.time).root  # the newest
    e.copy_round()
    assert len(e.snaps("second")) == 1  # and it doesn't keep trying the older ones
    assert quota_short(status_of(e.db, "second"))


def test_lowering_the_quota_of_a_full_copy_trims_it_and_it_does_not_copy_back(env):
    e = env()
    _fill(e, 4)
    e.copy_round()
    assert len(e.snaps("second")) == 4
    e.reconfigure(second_extra="max_gb = 2")  # the user sets a quota later
    e.copy_round()
    held = e.snaps("second")
    assert 1 <= len(held) < 4
    first = {s.root for s in held}
    e.copy_round()
    assert {s.root for s in e.snaps("second")} == first  # no churn
    assert quota_short(status_of(e.db, "second"))  # retention was cut short by the quota
    # The primary kept everything, so no clip is lost.
    assert len(e.snaps("local")) == 4


def test_a_lost_database_is_rebuilt_across_destinations_without_downloads(env):
    e = env()
    names = _fill(e, 3)
    e.copy_round()
    e.db = Db(e.tmp / "fresh.sqlite")
    e.copier.db = e.db
    e.cam.downloads.clear()
    assert e.backup().outcome == "ok"
    assert e.cam.downloads == []  # the primary's index brought every clip back
    e.copy_round()
    assert e.db.location_roots("cam", "second") == e.db.location_roots("cam", "local")
    assert len(e.snaps("second")) == 3  # and nothing was copied twice
    clips = e.db.clips_by_name("cam", names)
    assert {c.state for c in clips.values()} == {"backed_up"}
    for c in clips.values():
        assert e.db.clip_location(c, ["second"]) is not None
