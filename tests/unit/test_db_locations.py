"""The index's view of where each snapshot lives, and moving a v1 database."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from reovault.db import Db

T = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

V1_CLIPS = """
CREATE TABLE clips (
  id INTEGER PRIMARY KEY, camera TEXT NOT NULL, name TEXT NOT NULL, start_utc TEXT NOT NULL,
  end_utc TEXT, size INTEGER, rec_type TEXT, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_attempt_at TEXT,
  repository TEXT, snapshot_id TEXT, path TEXT, backed_up_at TEXT, first_seen_at TEXT NOT NULL,
  UNIQUE (camera, name, start_utc)
);
CREATE TABLE runs (id INTEGER PRIMARY KEY, camera TEXT NOT NULL, trigger TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, clips INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0, lost INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE alert_state (key TEXT PRIMARY KEY, condition TEXT NOT NULL, camera TEXT,
  title TEXT NOT NULL, message TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_sent_at TEXT);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _clip(db: Db, name: str, snapshot: str, size: int = 100) -> int:
    db.upsert_clips("cam", [(name, T, T + timedelta(seconds=30), size, "md")])
    (clip,) = db.clips_by_name("cam", [name]).values()
    db.mark_backed_up("cam", "local", snapshot, T, {clip.id: f"/batch/{name}.mp4"})
    return clip.id


def test_a_version_1_database_moves_up_and_keeps_its_clips(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(V1_CLIPS)
    old.execute(
        "INSERT INTO clips (camera, name, start_utc, state, repository, snapshot_id, "
        "first_seen_at) VALUES ('cam', 'c1', '2026-11-01T10:00:00Z', 'backed_up', '/repo', "
        "'snap1', 'x')"
    )
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()

    db = Db(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    (clip,) = db.clips_by_name("cam", ["c1"]).values()
    assert (clip.state, clip.snapshot_id) == ("backed_up", "snap1")
    assert db.location_roots("cam", "local") == set()  # filled in by the next run
    assert db.expire_unlocated("cam") == 0  # and nothing expires meanwhile
    db.add_location("cam", "snap1", "local", "snap1", T)
    assert db.location_roots("cam", "local") == {"snap1"}


def test_an_empty_listing_never_replaces_known_locations(tmp_path):
    db = Db(tmp_path / "x.db")
    _clip(db, "c1", "s1")
    assert db.set_locations("cam", "local", []) is False
    assert db.location_roots("cam", "local") == {"s1"}
    assert db.set_locations("cam", "local", [("s1", "s1", T), ("s2", "s2", T)]) is True
    assert db.location_roots("cam", "local") == {"s1", "s2"}


def test_a_clip_expires_only_when_no_destination_holds_it(tmp_path):
    db = Db(tmp_path / "x.db")
    _clip(db, "c1", "s1")
    db.add_location("cam", "s1", "cloud", "copy-of-s1", T)
    db.set_locations("cam", "local", [("other", "other", T)])  # local forgot s1
    assert db.expire_unlocated("cam") == 0  # the cloud still has it
    db.set_locations("cam", "cloud", [("other", "other2", T)])  # and now the cloud forgot too
    assert db.expire_unlocated("cam") == 1
    (clip,) = db.clips_by_name("cam", ["c1"]).values()
    assert clip.state == "expired"


def test_a_clip_is_read_from_the_most_preferred_destination_that_holds_it(tmp_path):
    db = Db(tmp_path / "x.db")
    cid = _clip(db, "c1", "s1")
    db.add_location("cam", "s1", "cloud", "cloud-id", T)
    clip = db.get_clip(cid)
    assert db.clip_location(clip, ["local", "cloud"]) == ("local", "s1")
    assert db.clip_location(clip, ["cloud", "local"]) == ("cloud", "cloud-id")
    assert db.clip_location(clip, ["cloud"]) == ("cloud", "cloud-id")
    assert db.clip_location(clip, ["elsewhere"]) is None  # a disconnected destination
    assert db.clip_location(clip, []) is None


def test_usage_sums_the_footage_each_snapshot_holds(tmp_path):
    db = Db(tmp_path / "x.db")
    _clip(db, "c1", "s1", size=100)
    _clip(db, "c2", "s1", size=50)
    _clip(db, "c3", "s2", size=7)
    usage = {u["root"]: u["bytes"] for u in db.snapshot_usage("local")}
    assert usage == {"s1": 150, "s2": 7}
    assert db.snapshot_usage("cloud") == []


def test_a_repository_once_seen_stays_seen(tmp_path):
    db = Db(tmp_path / "x.db")
    assert db.repo_seen("local", "cam") is False
    db.mark_repo_seen("local", "cam")
    assert db.repo_seen("local", "cam") is True
    assert db.repo_seen("cloud", "cam") is False  # per destination


def test_forgetting_a_destination_clears_only_its_own_memory(tmp_path):
    db = Db(tmp_path / "x.db")
    for dest in ("old", "other"):
        db.add_location("cam", "s1", dest, "x", T)
        db.mark_repo_seen(dest, "cam")
        db.set_setting(f"copy_floor:{dest}", "2026-01-01T00:00:00Z")
    db.forget_destination("old")
    assert db.location_roots("cam", "old") == set() and not db.repo_seen("old", "cam")
    assert db.get_setting("copy_floor:old") is None
    assert db.location_roots("cam", "other") == {"s1"} and db.repo_seen("other", "cam")


def test_a_run_cut_off_by_a_restart_is_stopped_not_failed(tmp_path):
    db = Db(tmp_path / "x.db")
    db.start_run("cam", "schedule")
    db.abort_unfinished_runs()
    (run,) = db.runs("cam", limit=1, finished=True)
    assert run.outcome == "stopped"
