from datetime import UTC, datetime

import pytest

from reovault.models import RemoteRecording
from reovault.providers.fake import FakeProvider, ScriptedRecording


def _recording(name: str, start: datetime) -> RemoteRecording:
    return RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=1024,
        raw_metadata={"fileName": name},
    )


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
