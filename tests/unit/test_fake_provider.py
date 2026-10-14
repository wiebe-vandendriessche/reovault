from datetime import UTC, datetime

import pytest

from reovault.providers.fake import FakeProvider, ScriptedRecording
from tests.unit.conftest import make_recording as _recording


def test_list_recordings_filters_by_window():
    provider = FakeProvider(
        recordings=[
            ScriptedRecording(_recording("a.mp4", datetime(2026, 1, 1, tzinfo=UTC)), b"a"),
            ScriptedRecording(_recording("b.mp4", datetime(2026, 6, 1, tzinfo=UTC)), b"b"),
        ]
    )
    result = provider.list_recordings(
        from_utc=datetime(2026, 5, 1, tzinfo=UTC), to_utc=datetime(2026, 7, 1, tzinfo=UTC)
    )
    assert [r.remote_name for r in result] == ["b.mp4"]


def test_fetch_writes_content_and_reports_bytes(tmp_path):
    rec = _recording("clip.mp4", datetime(2026, 1, 1, tzinfo=UTC))
    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"hello world")])
    dest = tmp_path / "staging" / "clip.mp4"
    result = provider.fetch(rec, str(dest))
    assert dest.read_bytes() == b"hello world"
    assert result.bytes_written == 11
    assert provider.fetch_calls == 1


def test_fetch_can_simulate_truncated_download(tmp_path):
    rec = _recording("clip.mp4", datetime(2026, 1, 1, tzinfo=UTC))
    provider = FakeProvider(
        recordings=[ScriptedRecording(rec, b"full content", on_fetch=lambda: b"trunc")]
    )
    result = provider.fetch(rec, str(tmp_path / "clip.mp4"))
    # The archiver's size check catches this; the fake provider's job is only
    # to be scriptable, not to validate.
    assert result.bytes_written == 5


def test_fetch_unknown_recording_raises_keyerror(tmp_path):
    provider = FakeProvider(recordings=[])
    rec = _recording("missing.mp4", datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(KeyError):
        provider.fetch(rec, str(tmp_path / "missing.mp4"))


def test_storage_status_default():
    provider = FakeProvider()
    status = provider.storage_status()
    assert status.mounted is True
    assert status.formatted is True


def test_list_error_raises_once_then_clears():
    provider = FakeProvider(list_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        provider.list_recordings(
            from_utc=datetime(2026, 1, 1, tzinfo=UTC), to_utc=datetime(2026, 1, 2, tzinfo=UTC)
        )
    # cleared: a second call succeeds
    assert (
        provider.list_recordings(
            from_utc=datetime(2026, 1, 1, tzinfo=UTC), to_utc=datetime(2026, 1, 2, tzinfo=UTC)
        )
        == []
    )


def test_demo_provider_card_slides_with_the_clock(tmp_path):
    from datetime import timedelta

    from reovault.providers.base import RecordingGoneError
    from reovault.providers.fake import DemoProvider

    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    demo = DemoProvider(now=lambda: now)
    week = demo.list_recordings(from_utc=now - timedelta(days=30), to_utc=now)
    assert len(week) == 7 * 24 * 4 + 1  # one per 15 min, both ends included
    assert week[0].start_utc == now - timedelta(days=7)

    later = now + timedelta(hours=1)
    demo.now = lambda: later
    moved = demo.list_recordings(from_utc=later - timedelta(days=30), to_utc=later)
    assert moved[0].start_utc == week[4].start_utc  # 4 oldest clips rolled off
    assert moved[-1].start_utc == later

    def is_gone(r):
        return round(r.start_utc.timestamp() / 900) % demo.gone_every == 0

    gone = next(r for r in moved if is_gone(r))
    with pytest.raises(RecordingGoneError):
        demo.fetch(gone, str(tmp_path / "g.mp4"))
    kept = next(r for r in moved if not is_gone(r))
    assert demo.fetch(kept, str(tmp_path / "k.mp4")).bytes_written == kept.remote_size
