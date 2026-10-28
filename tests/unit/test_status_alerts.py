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
