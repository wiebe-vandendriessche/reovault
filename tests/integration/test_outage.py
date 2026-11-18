"""A destination that stops answering, and comes back: real restic and rclone,
an rclone remote switched between a local folder and a WebDAV address nobody
listens on (as when Nextcloud went away in November 2026)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from reovault.backup import BackupRun, CameraState
from reovault.db import Db
from reovault.restic import Restic
from tests.fake_camera import FakeCamera

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    restic, rclone = shutil.which("restic"), shutil.which("rclone")
    if restic is None or rclone is None:
        pytest.fail("restic and rclone must be on PATH (see CONTRIBUTING.md)")
    conf = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    (tmp_path / "pw").write_text("test-password")

    class Env:
        cam = FakeCamera()
        db = Db(tmp_path / "reovault.db")
        state = CameraState()
        repo = Restic.for_camera(
            restic,
            f"rclone:nc:{tmp_path / 'remote'}",
            "cam",
            {"RESTIC_PASSWORD_FILE": str(tmp_path / "pw"), "RESTIC_CACHE_DIR": str(tmp_path / "c")},
        )

        def up(self) -> None:
            conf.write_text("[nc]\ntype = local\n")

        def down(self) -> None:
            conf.write_text("[nc]\ntype = webdav\nurl = http://127.0.0.1:9/\n")

        def run(self):
            return BackupRun(
                camera=self.cam,
                restic=self.repo,
                db=self.db,
                staging=tmp_path / "staging" / "cam",
                state=self.state,
                interval=timedelta(hours=1),
                destination="nc",
                sleep=lambda _s: None,
            ).run("schedule")

    e = Env()
    e.up()
    return e


def test_an_unreachable_destination_is_an_outage_and_backups_resume_after_it(env):
    env.cam.add(NOW - timedelta(hours=3))
    assert env.run().outcome == "ok"

    env.down()
    late = env.cam.add(NOW - timedelta(minutes=30))
    result = env.run()
    assert result.outcome == "error"
    assert env.state.destination_down
    assert env.db.get_setting("down_since:nc")
    # The card was still read: the new clip is known and waits for the destination.
    assert env.db.clips_by_name("cam", [late])[late].state == "pending"

    env.up()
    assert env.run().outcome == "ok"
    assert env.db.clips_by_name("cam", [late])[late].state == "backed_up"
    assert not env.state.destination_down
    assert env.db.get_setting("down_since:nc") is None


def test_a_destination_that_never_answers_is_never_created_again(env):
    env.cam.add(NOW - timedelta(hours=3))
    assert env.run().outcome == "ok"
    env.down()
    env.run()
    # An outage is not "the repository is gone": nothing was (re)initialised.
    assert env.db.repo_seen("nc", "cam")
    env.up()
    assert len(env.repo.snapshots()) == 1
