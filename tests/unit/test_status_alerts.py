"""Per-camera verdicts and the state-based notifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reovault.alerts import Channel, Condition, Message, Notifier
from reovault.backup import CameraState
from reovault.camera import CardStatus
from reovault.config import Settings
from reovault.db import Db
from reovault.status import camera_status

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _db(tmp_path) -> Db:
    return Db(tmp_path / "db.sqlite")


def _clip(db: Db, hours_ago: float, size: int = 1000) -> None:
    start = NOW - timedelta(hours=hours_ago)
    db.upsert_clips("cam", [(f"c{hours_ago}", start, None, size, "md")])


def test_no_run_yet_is_unknown(tmp_path):
    assert camera_status("cam", _db(tmp_path), None, NOW).verdict == "unknown"


def test_waiting_clips_are_catching_up_unless_near_the_overwrite_edge(tmp_path):
    db = _db(tmp_path)
    db.finish_run(db.start_run("cam", "schedule"), "ok")
    _clip(db, 10)
    card = CameraState(oldest_on_card=NOW - timedelta(days=7))
    assert camera_status("cam", db, card, NOW).verdict == "catching_up"
    _clip(db, 24 * 6.5)  # in the oldest fifth of a 7-day card
    assert camera_status("cam", db, card, NOW).verdict == "at_risk"


def test_a_failed_run_or_missing_card_wins(tmp_path):
    db = _db(tmp_path)
    db.finish_run(db.start_run("cam", "schedule"), "error", "camera unreachable")
    status = camera_status("cam", db, None, NOW)
    assert (status.verdict, status.last_error) == ("failing", "camera unreachable")
    no_card = CameraState(card=CardStatus(None, None, mounted=False))
    assert camera_status("cam", db, no_card, NOW).verdict == "not_mounted"


class _Recorder(Channel):
    name = "test"

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, msg: Message) -> None:
        self.sent.append(msg)


def test_an_alert_opens_once_renotifies_and_resolves(tmp_path):
    db = _db(tmp_path)
    channel = _Recorder()
    now = [NOW]
    current = [Condition("failing", "cam", "cam: backups are failing", "boom")]
    notifier = Notifier(
        settings=Settings,
        db=db,
        conditions=lambda: current,
        channels=[channel],
        clock=lambda: now[0],
    )
    notifier.evaluate()
    notifier.evaluate()
    assert [m.title for m in channel.sent] == ["cam: backups are failing"]
    now[0] += timedelta(hours=25)  # renotify_hours defaults to 24
    notifier.evaluate()
    assert len(channel.sent) == 2
    current.clear()
    notifier.evaluate()
    assert channel.sent[-1].resolved and channel.sent[-1].title.startswith("Resolved:")
    assert db.list_alerts() == []


def test_a_run_in_progress_does_not_resolve_a_failing_alert(tmp_path):
    db = _db(tmp_path)
    for _ in range(2):
        db.finish_run(db.start_run("cam", "schedule"), "error", "boom")
    db.start_run("cam", "schedule")  # in progress: no outcome yet
    runs = db.runs("cam", limit=2, finished=True)
    assert [r.outcome for r in runs] == ["error", "error"]


def test_a_first_run_in_progress_does_not_claim_everything_is_backed_up(tmp_path):
    db = _db(tmp_path)
    db.start_run("cam", "schedule")  # running, card not read yet: no clips known
    status = camera_status("cam", db, CameraState())
    assert status.verdict == "unknown"


def _runtime(tmp_path):
    from reovault.camera import Gateway
    from reovault.config_store import ConfigStore
    from reovault.runtime import Runtime

    (tmp_path / "c.toml").write_text(
        f"""
[[destinations]]
name = "local"
repository = "{tmp_path / "a"}"
primary = true
max_gb = 10

[[destinations]]
name = "cloud"
repository = "{tmp_path / "b"}"
max_gb = 10
"""
    )
    from reovault.config import load_settings

    return Runtime(
        config=ConfigStore(tmp_path / "c.toml", load_settings(tmp_path / "c.toml")),
        db=_db(tmp_path),
        gateway=Gateway("x", "127.0.0.1:1"),
    )


def test_destination_alerts_say_what_is_wrong(tmp_path):
    from reovault.destinations import update_status

    rt = _runtime(tmp_path)
    assert rt._destination_conditions(rt.settings()) == []  # healthy: nothing to say
    update_status(rt.db, "cloud", failures=2, error="connection refused", too_small=True)
    update_status(rt.db, "local", blocked=3)
    kinds = {c.condition for c in rt._destination_conditions(rt.settings())}
    assert kinds == {"destination_failing", "quota_too_small", "quota_blocked"}


def test_a_single_failure_is_not_yet_an_alert(tmp_path):
    from reovault.destinations import update_status

    rt = _runtime(tmp_path)
    update_status(rt.db, "cloud", failures=1, error="timeout")
    assert rt._destination_conditions(rt.settings()) == []


def test_a_copy_that_stays_behind_alerts_but_not_while_it_is_seeding(tmp_path):
    from reovault.destinations import update_status

    rt = _runtime(tmp_path)
    old = NOW - timedelta(days=2)
    rt.db.upsert_clips("cam", [("c1", old, None, 100, "md")])
    (clip,) = rt.db.clips_by_name("cam", ["c1"]).values()
    rt.db.mark_backed_up("cam", "local", "s1", old, {clip.id: "/b/c1.mp4"})
    rt.db.set_locations("cam", "local", [("s1", "s1", old)])
    kinds = [c.condition for c in rt._destination_conditions(rt.settings())]
    assert kinds == ["copy_behind"]
    update_status(rt.db, "cloud", seeding={"cam": [1, 5]})  # progress: copying right now
    assert rt._destination_conditions(rt.settings()) == []
