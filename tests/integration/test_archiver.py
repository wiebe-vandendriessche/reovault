"""Archiver state-machine, retry/backoff classification, dedup, and
single-instance-lock tests. See plan: Reliability."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from reovault.archiver import AlreadyRunningError, instance_lock
from reovault.models import RemoteRecording
from reovault.providers.base import (
    AuthError,
    DeviceError,
    InputError,
    NetworkError,
    ProtocolError,
)
from reovault.providers.fake import FakeProvider, ScriptedRecording

WINDOW_FROM = datetime(2026, 4, 17, 0, 0, 0, tzinfo=UTC)
WINDOW_TO = datetime(2026, 4, 17, 23, 59, 59, tzinfo=UTC)


def _rec(name: str, start: datetime, size: int) -> RemoteRecording:
    return RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=size,
        raw_metadata={"name": name},
    )


def _no_backoff():
    """Patches compute_backoff to zero delay so retry tests don't need to
    manipulate wall-clock time to make a row "due" again."""
    return patch("reovault.archiver.compute_backoff", return_value=timedelta(seconds=0))


def test_happy_path_archives_one_recording(env):
    content = b"fake video bytes" * 10
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), len(content))
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.discovered == 1
    assert result.downloaded == 1
    assert result.failed == 0
    assert result.bytes_archived == len(content)

    rows = env.repository.conn.execute("SELECT * FROM recordings").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "archived"
    vault_path = rows[0]["vault_path"]
    assert env.vault.verify(vault_path) is True

    out = env.tmp_path / "exported.mp4"
    env.vault.get(vault_path, out)
    assert out.read_bytes() == content

    # staging is cleaned up after a successful run
    if env.staging_dir.exists():
        assert list(env.staging_dir.rglob("*")) == []


def test_dedup_under_overlapping_windows(env):
    content = b"x" * 100
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), len(content))
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    r1 = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    r2 = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    r3 = archiver.run(
        trigger="manual",
        from_utc=WINDOW_FROM - timedelta(hours=6),
        to_utc=WINDOW_TO + timedelta(hours=6),
    )

    assert r1.downloaded == 1
    assert r2.downloaded == 0
    assert r2.skipped_dup == 1
    assert r3.downloaded == 0
    assert r3.skipped_dup == 1

    rows = env.repository.conn.execute("SELECT * FROM recordings").fetchall()
    assert len(rows) == 1  # exactly one row, never duplicated
    assert provider.fetch_calls == 1  # downloaded exactly once


def test_size_mismatch_is_quarantined_not_retried(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 999999)  # wrong expected size
    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"short content")])
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.failed == 1
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "quarantined"
    assert row["last_error_class"] == "protocol"


@pytest.mark.parametrize(
    "error_cls,message",
    [(NetworkError, "timeout"), (DeviceError, "device busy")],
)
def test_retryable_error_sets_failed_with_backoff(env, error_cls, message):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)

    def _boom():
        raise error_cls(message)

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_boom)])
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.failed == 1
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None


def test_network_error_eventually_quarantines_after_max_attempts(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)

    def _boom():
        raise NetworkError("still down")

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_boom)])
    archiver = env.archiver(provider)

    with _no_backoff():
        for _ in range(8):
            archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "quarantined"
    assert row["attempts"] == 8


def test_network_error_recovers_on_a_later_run(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise NetworkError("flaky link")
        return None  # succeed with the scripted content

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_flaky)])
    archiver = env.archiver(provider)

    with _no_backoff():
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
        result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.downloaded == 1
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "archived"


def test_device_error_retries_once_then_quarantines(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)

    def _boom():
        raise DeviceError("firmware weirdness")

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_boom)])
    archiver = env.archiver(provider)

    with _no_backoff():
        r1 = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
        row_after_first = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
        assert r1.failed == 1
        assert row_after_first["state"] == "failed"  # first attempt: still retryable

        r2 = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
        row_after_second = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
        assert r2.failed == 1
        assert row_after_second["state"] == "quarantined"
        assert row_after_second["attempts"] == 2


@pytest.mark.parametrize("error_cls", [InputError, ProtocolError])
def test_input_and_protocol_errors_quarantine_immediately(env, error_cls):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)

    def _boom():
        raise error_cls("bad shape")

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_boom)])
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.failed == 1
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "quarantined"
    assert row["attempts"] == 1  # never retried


def test_auth_error_aborts_the_whole_run(env):
    rec1 = _rec("clip1", datetime(2026, 4, 17, 10, tzinfo=UTC), 10)
    rec2 = _rec("clip2", datetime(2026, 4, 17, 14, tzinfo=UTC), 10)

    def _boom():
        raise AuthError("invalid credentials")

    provider = FakeProvider(
        recordings=[
            ScriptedRecording(rec1, b"0123456789", on_fetch=_boom),
            ScriptedRecording(rec2, b"0123456789"),
        ]
    )
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.downloaded == 0
    # rec2 must never even be attempted: auth stops the run immediately.
    # Discovery and per-item processing are interleaved, so rec2 (later in
    # the list) never even gets its `discover_recording` insert; nothing is
    # lost either way, since it's still on the camera and will be
    # rediscovered on the very next run.
    assert provider.fetch_calls == 1
    row2 = env.repository.conn.execute(
        "SELECT * FROM recordings WHERE remote_name = 'clip2'"
    ).fetchone()
    assert row2 is None

    run_row = env.repository.conn.execute("SELECT * FROM archive_runs").fetchone()
    assert run_row["outcome"] == "aborted"


def test_auth_error_at_discovery_aborts_before_any_processing(env):
    provider = FakeProvider(list_error=AuthError("session expired"))
    archiver = env.archiver(provider)

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    assert result.discovered == 0
    run_row = env.repository.conn.execute("SELECT * FROM archive_runs").fetchone()
    assert run_row["outcome"] == "aborted"


def test_generic_provider_error_at_discovery_marks_run_failed(env):
    provider = FakeProvider(list_error=NetworkError("device unreachable"))
    archiver = env.archiver(provider)

    archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    run_row = env.repository.conn.execute("SELECT * FROM archive_runs").fetchone()
    assert run_row["outcome"] == "failed"


def test_single_instance_lock_blocks_concurrent_runs(env):
    with instance_lock(env.lock_path):
        provider = FakeProvider()
        archiver = env.archiver(provider)
        with pytest.raises(AlreadyRunningError):
            archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)


def test_local_error_during_fetch_aborts_without_mutating_state(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)

    def _boom():
        raise OSError("disk full")

    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789", on_fetch=_boom)])
    archiver = env.archiver(provider)

    archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    # Never mutated past the pre-download transition: this is OUR failure,
    # not the recording's, per plan: "Never mutate recording state on a
    # storage failure."
    assert row["state"] == "downloading"
    run_row = env.repository.conn.execute("SELECT * FROM archive_runs").fetchone()
    assert run_row["outcome"] == "aborted"


def test_local_error_during_vault_put_aborts_without_finalizing(env):
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), 10)
    provider = FakeProvider(recordings=[ScriptedRecording(rec, b"0123456789")])
    archiver = env.archiver(provider)

    with patch.object(env.vault, "put", side_effect=OSError("disk full")):
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "verifying"  # stuck one step short of archived
    run_row = env.repository.conn.execute("SELECT * FROM archive_runs").fetchone()
    assert run_row["outcome"] == "aborted"
