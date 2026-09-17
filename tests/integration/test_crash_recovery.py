"""Crash-injection tests. See plan: Testing #3 ("the most valuable test in
the suite") and Verification #3: kill at each state-machine boundary, then
assert the invariant holds after recovery: no `archived` row lacks a
verified file, and no recording is ever stored twice.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from reovault.models import RemoteRecording
from reovault.providers.fake import FakeProvider, ScriptedRecording

WINDOW_FROM = datetime(2026, 4, 17, 0, 0, 0, tzinfo=UTC)
WINDOW_TO = datetime(2026, 4, 17, 23, 59, 59, tzinfo=UTC)


def _rec(name: str, start: datetime, content: bytes) -> tuple[RemoteRecording, bytes]:
    return (
        RemoteRecording(
            remote_name=name,
            start_utc=start,
            end_utc=None,
            duration_s=None,
            rec_type="md",
            stream="main",
            remote_size=len(content),
            raw_metadata={"name": name},
        ),
        content,
    )


def _assert_invariant_holds(env):
    """The core invariant: a row is `archived` only if a fully-written,
    verified file exists at `vault_path`, and every archived vault_path is
    unique (no recording stored twice)."""
    rows = env.repository.conn.execute(
        "SELECT * FROM recordings WHERE state = 'archived'"
    ).fetchall()
    vault_paths = [r["vault_path"] for r in rows]
    assert len(vault_paths) == len(set(vault_paths)), "a vault_path is claimed by more than one row"
    for row in rows:
        assert row["vault_path"] is not None
        assert row["plaintext_sha256"] is not None
        assert env.vault.verify(row["vault_path"]) is True


def test_crash_between_downloading_and_fetch_completing(env):
    """Kill right after `transition_downloading` commits but before the
    download itself does anything (the row is stuck `downloading` with no
    vault file at all)."""
    rec, content = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), b"video bytes" * 5)
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    with (
        patch.object(provider, "fetch", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "downloading"  # orphaned, as expected

    archiver.reconcile()
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "failed"  # requeued, due immediately

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    assert result.downloaded == 1
    _assert_invariant_holds(env)
    assert provider.fetch_calls == 1  # only the post-reconcile attempt actually called fetch


def test_crash_between_vault_rename_and_finalize_commit(env):
    """Kill right after the ciphertext is atomically renamed into place but
    before `finalize_archived` commits. The vault file is real, complete,
    and verifiable; the DB row is stuck `verifying`. Reconciliation must
    adopt it rather than re-download."""
    rec, content = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), b"video bytes" * 5)
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    with (
        patch.object(
            env.repository, "finalize_archived", side_effect=RuntimeError("simulated crash")
        ),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "verifying"
    # The vault file genuinely exists already, unreferenced by any archived row.
    orphans = env.vault.list_vault_paths()
    assert len(orphans) == 1

    archiver.reconcile()

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "archived"
    assert row["vault_path"] == orphans[0]
    _assert_invariant_holds(env)

    # Re-running the same window must NOT re-download: the row is archived.
    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    assert result.downloaded == 0
    assert result.skipped_dup == 1
    assert provider.fetch_calls == 1  # never re-fetched
    _assert_invariant_holds(env)


def test_reconcile_deletes_a_corrupt_orphan_instead_of_adopting_it(env):
    rec, content = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), b"video bytes" * 5)
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    with (
        patch.object(
            env.repository, "finalize_archived", side_effect=RuntimeError("simulated crash")
        ),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    orphan_path = env.vault.list_vault_paths()[0]
    ciphertext_path = env.vault.vault_dir / orphan_path
    data = bytearray(ciphertext_path.read_bytes())
    data[-1] ^= 0xFF  # tamper: this "orphan" is actually corrupt
    ciphertext_path.write_bytes(bytes(data))

    archiver.reconcile()

    assert env.vault.list_vault_paths() == []  # deleted, not adopted
    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "failed"  # requeued by the stuck-row sweep, ready to retry

    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    assert result.downloaded == 1  # re-downloaded cleanly
    _assert_invariant_holds(env)


def test_reconcile_deletes_unattributable_vault_file(env):
    stray = env.vault.vault_dir / "doorbell" / "2026" / "04" / "17"
    stray.mkdir(parents=True)
    (stray / "not-a-real-vault-file.enc").write_bytes(b"garbage")

    archiver = env.archiver(FakeProvider())
    archiver.reconcile()

    assert env.vault.list_vault_paths() == []


def test_reconcile_sweeps_orphaned_staging_files(env):
    leftover_dir = env.staging_dir / "999"
    leftover_dir.mkdir(parents=True)
    (leftover_dir / "half-downloaded.mp4").write_bytes(b"partial")
    env.staging_dir.joinpath("stray-loose-file.tmp").write_bytes(b"orphan")

    archiver = env.archiver(FakeProvider())
    archiver.reconcile()

    assert list(env.staging_dir.iterdir()) == []


def test_reconcile_is_a_noop_on_a_clean_vault(env):
    rec, content = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), b"video bytes" * 5)
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    archiver.reconcile()  # must not disturb the already-correct archived row

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "archived"
    _assert_invariant_holds(env)


def test_multiple_crashes_across_multiple_recordings_converge_correctly(env):
    """A broader version of the invariant check across several recordings,
    some crashing at different boundaries, then one reconcile + one more
    run should leave everything archived exactly once."""
    specs = [
        _rec("clip-a", datetime(2026, 4, 17, 8, tzinfo=UTC), b"AAAA" * 5),
        _rec("clip-b", datetime(2026, 4, 17, 10, tzinfo=UTC), b"BBBB" * 5),
        _rec("clip-c", datetime(2026, 4, 17, 12, tzinfo=UTC), b"CCCC" * 5),
    ]
    provider = FakeProvider(recordings=[ScriptedRecording(r, c) for r, c in specs])
    archiver = env.archiver(provider)

    # Crash on the very first finalize_archived call (affects clip-a only;
    # clip-b/clip-c are processed afterward in the same run and succeed).
    real_finalize = env.repository.finalize_archived
    call_count = {"n": 0}

    def _flaky_finalize(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash")
        return real_finalize(*args, **kwargs)

    with (
        patch.object(env.repository, "finalize_archived", side_effect=_flaky_finalize),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    archiver.reconcile()
    result = archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)

    rows = env.repository.conn.execute("SELECT * FROM recordings").fetchall()
    assert len(rows) == 3
    assert all(r["state"] == "archived" for r in rows)
    _assert_invariant_holds(env)
    assert result.skipped_dup >= 0  # clip-b/clip-c already archived by the first run
