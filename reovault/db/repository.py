"""All SQL lives here, the only module that touches `sqlite3` directly (see
plan: Repository layout). Implements the dedup contract and the archive state
machine's transitions (see plan: Reliability).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from reovault.models import ErrorClass, RemoteRecording

_ISO = "%Y-%m-%dT%H:%M:%S.%f"


def _now() -> str:
    return datetime.now(UTC).strftime(_ISO)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_ISO)


def parse_iso_utc(s: str) -> datetime:
    """Inverse of `_iso`. Public because the archiver needs to reconstruct a
    `RemoteRecording` from a stored `RecordingRow` for retry processing."""
    return datetime.strptime(s, _ISO).replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StorageSampleRow:
    total_gb: float | None
    remain_gb: float | None
    formatted: bool | None
    mounted: bool | None
    oldest_recording_utc: str | None
    sampled_at: str


@dataclass(frozen=True, slots=True)
class RecordingRow:
    id: int
    device_id: int
    channel: int
    remote_name: str
    start_utc: str
    remote_size: int | None
    state: str
    vault_path: str | None
    plaintext_sha256: str | None
    attempts: int
    next_attempt_at: str | None


class Repository:
    """One instance per SQLite database file. Opens with WAL journaling (single
    writer, embedded, no infra, see plan: Data model) and enforces foreign
    keys, which SQLite otherwise leaves off by default.

    `check_same_thread=False` plus an internal lock serializing every access:
    Phase 5 put the scheduler (its own background thread) and the `/healthz`
    web app (its own threadpool thread, via Starlette's sync-route dispatch)
    in the same process, sharing one `Archiver` and therefore one
    `Repository`. Python's sqlite3 module refuses cross-thread use of a
    connection by default (raises `ProgrammingError`), and even with that
    check disabled, interleaving unrelated statements on one connection from
    multiple threads without synchronization is not safe. The lock makes
    this Repository what SQLite's own single-writer model already assumes:
    one operation at a time, from wherever it's called."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        # executescript() runs as DDL autocommit and would otherwise collide
        # with the explicit BEGIN/COMMIT `transaction()` uses elsewhere.
        schema_sql = resources.files("reovault.db").joinpath("schema.sql").read_text()
        self.conn.executescript(schema_sql)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _fetchone(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        """Lock-protected read, for methods that query outside of
        `transaction()` (a plain SELECT never needs BEGIN/COMMIT, but it
        still needs the same cross-thread serialization)."""
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
            return row

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- devices --------------------------------------------------------

    def upsert_device(
        self, *, alias: str, channel: int, timezone: str, model: str | None = None
    ) -> int:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO devices (alias, channel, model, timezone)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(alias, channel) DO UPDATE SET
                    model = COALESCE(excluded.model, devices.model),
                    timezone = excluded.timezone
                """,
                (alias, channel, model, timezone),
            )
            row = conn.execute(
                "SELECT id FROM devices WHERE alias = ? AND channel = ?", (alias, channel)
            ).fetchone()
            return int(row["id"])

    def get_device_id(self, *, alias: str, channel: int) -> int | None:
        row = self._fetchone(
            "SELECT id FROM devices WHERE alias = ? AND channel = ?", (alias, channel)
        )
        return int(row["id"]) if row else None

    # -- recordings / dedup ----------------------------------------------

    def discover_recording(
        self, *, device_id: int, channel: int, recording: RemoteRecording
    ) -> tuple[int, bool]:
        """The dedup mechanism: `INSERT OR IGNORE` on the unique
        `(device_id, channel, remote_name, start_utc)` key. Overlapping scan
        windows are absorbed here, not by application-level checking. See
        plan: "the database, not application logic, guarantees a recording is
        never written twice." Returns `(recording_id, created)`."""
        start_utc = _iso(recording.start_utc)
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO recordings (
                    device_id, channel, remote_name, start_utc, end_utc, duration_s,
                    rec_type, stream, remote_size, state, first_seen_at, raw_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                """,
                (
                    device_id,
                    channel,
                    recording.remote_name,
                    start_utc,
                    _iso(recording.end_utc) if recording.end_utc else None,
                    recording.duration_s,
                    recording.rec_type,
                    recording.stream,
                    recording.remote_size,
                    _now(),
                    json.dumps(recording.raw_metadata),
                ),
            )
            created = cur.rowcount > 0
            row = conn.execute(
                """
                SELECT id FROM recordings
                WHERE device_id = ? AND channel = ? AND remote_name = ? AND start_utc = ?
                """,
                (device_id, channel, recording.remote_name, start_utc),
            ).fetchone()
            return int(row["id"]), created

    def get(self, recording_id: int) -> RecordingRow | None:
        row = self._fetchone("SELECT * FROM recordings WHERE id = ?", (recording_id,))
        return self._row_to_recording(row) if row else None

    def find_by_hash(self, plaintext_sha256: str) -> RecordingRow | None:
        row = self._fetchone(
            "SELECT * FROM recordings WHERE plaintext_sha256 = ? AND state = 'archived'",
            (plaintext_sha256,),
        )
        return self._row_to_recording(row) if row else None

    def find_archived_by_vault_path(self, vault_path: str) -> RecordingRow | None:
        """Used by reconciliation to tell an already-tracked vault file apart
        from an orphan (see plan: Reliability, crash between rename and the
        `finalize_archived` commit)."""
        row = self._fetchone(
            "SELECT * FROM recordings WHERE vault_path = ? AND state = 'archived'",
            (vault_path,),
        )
        return self._row_to_recording(row) if row else None

    def pending(self, *, device_id: int, now: datetime | None = None) -> list[RecordingRow]:
        """Everything still needing an attempt: freshly `discovered` rows
        (never attempted, e.g. left behind by a run that crashed before
        reaching them) plus `failed` rows whose backoff has elapsed."""
        now_iso = _iso(now) if now else _now()
        rows = self._fetchall(
            """
            SELECT * FROM recordings
            WHERE device_id = ?
                AND (state = 'discovered'
                     OR (state = 'failed' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)))
            """,
            (device_id, now_iso),
        )
        return [self._row_to_recording(r) for r in rows]

    def due_for_retry(self, *, device_id: int, now: datetime | None = None) -> list[RecordingRow]:
        now_iso = _iso(now) if now else _now()
        rows = self._fetchall(
            """
            SELECT * FROM recordings
            WHERE device_id = ? AND state = 'failed'
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (device_id, now_iso),
        )
        return [self._row_to_recording(r) for r in rows]

    def find_by_natural_key(
        self, *, device_id: int, channel: int, remote_name: str, start_utc: datetime
    ) -> RecordingRow | None:
        """Recovers the row a vault path belongs to, for reconciliation
        adopting an orphaned file after a crash between the atomic rename
        and the `finalize_archived` commit (see plan: Reliability)."""
        row = self._fetchone(
            """
            SELECT * FROM recordings
            WHERE device_id = ? AND channel = ? AND remote_name = ? AND start_utc = ?
            """,
            (device_id, channel, remote_name, _iso(start_utc)),
        )
        return self._row_to_recording(row) if row else None

    def stuck_in_progress(self, *, device_id: int) -> list[RecordingRow]:
        """Rows left in `downloading`/`verifying` by a crash mid-run (see
        plan: state-machine steps 2-6). Reconciliation requeues these."""
        rows = self._fetchall(
            """
            SELECT * FROM recordings
            WHERE device_id = ? AND state IN ('downloading', 'verifying')
            """,
            (device_id,),
        )
        return [self._row_to_recording(r) for r in rows]

    def requeue_after_crash(self, recording_id: int) -> None:
        """Resets a row orphaned by a crash back to `failed` with
        `next_attempt_at` in the past, so the very next run's due-retry sweep
        picks it up immediately instead of it sitting stuck forever."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'failed', last_error = 'requeued by reconciliation after a crash',
                    last_error_class = 'local', next_attempt_at = ?
                WHERE id = ?
                """,
                (_now(), recording_id),
            )

    # -- state machine ----------------------------------------------------
    # See plan: "no failure can lose an archived recording and no crash can
    # mark an unfinished one as archived." Each transition is its own commit.

    def transition_downloading(self, recording_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE recordings SET state = 'downloading', attempts = attempts + 1 WHERE id = ?",
                (recording_id,),
            )

    def transition_verifying(self, recording_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE recordings SET state = 'verifying' WHERE id = ?", (recording_id,))

    def finalize_archived(
        self,
        recording_id: int,
        *,
        vault_path: str,
        plaintext_sha256: str,
        plaintext_size: int,
        ciphertext_size: int,
    ) -> None:
        """Only ever called after the vault file is fully written, fsynced,
        and atomically renamed into place, see plan step 6 to 7. A row is
        `archived` only if that already happened; this call never itself
        writes the file."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'archived', vault_path = ?, plaintext_sha256 = ?,
                    plaintext_size = ?, ciphertext_size = ?, archived_at = ?,
                    last_error = NULL, last_error_class = NULL, next_attempt_at = NULL
                WHERE id = ?
                """,
                (
                    vault_path,
                    plaintext_sha256,
                    plaintext_size,
                    ciphertext_size,
                    _now(),
                    recording_id,
                ),
            )

    def mark_failed(
        self,
        recording_id: int,
        *,
        error: str,
        error_class: ErrorClass,
        next_attempt_at: datetime | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'failed', last_error = ?, last_error_class = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (
                    error,
                    error_class.value,
                    _iso(next_attempt_at) if next_attempt_at else None,
                    recording_id,
                ),
            )

    def mark_quarantined(self, recording_id: int, *, error: str, error_class: ErrorClass) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'quarantined', last_error = ?, last_error_class = ?,
                    next_attempt_at = NULL
                WHERE id = ?
                """,
                (error, error_class.value, recording_id),
            )

    # -- runs ---------------------------------------------------------------

    def start_run(
        self, *, device_id: int, trigger: str, window_from_utc: datetime, window_to_utc: datetime
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO archive_runs
                    (device_id, trigger, window_from_utc, window_to_utc, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, trigger, _iso(window_from_utc), _iso(window_to_utc), _now()),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        outcome: str,
        discovered: int = 0,
        downloaded: int = 0,
        skipped_dup: int = 0,
        failed: int = 0,
        bytes_archived: int = 0,
        error: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE archive_runs SET
                    finished_at = ?, outcome = ?, discovered = ?, downloaded = ?,
                    skipped_dup = ?, failed = ?, bytes_archived = ?, error = ?
                WHERE id = ?
                """,
                (
                    _now(),
                    outcome,
                    discovered,
                    downloaded,
                    skipped_dup,
                    failed,
                    bytes_archived,
                    error,
                    run_id,
                ),
            )

    def record_storage_sample(
        self,
        *,
        device_id: int,
        total_gb: float | None,
        remain_gb: float | None,
        formatted: bool | None,
        mounted: bool | None,
        oldest_recording_utc: datetime | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO device_storage_samples (
                    device_id, sampled_at, total_gb, remain_gb,
                    formatted, mounted, oldest_recording_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    _now(),
                    total_gb,
                    remain_gb,
                    formatted,
                    mounted,
                    _iso(oldest_recording_utc) if oldest_recording_utc else None,
                ),
            )

    def latest_storage_sample(self, device_id: int) -> StorageSampleRow | None:
        row = self._fetchone(
            """
            SELECT * FROM device_storage_samples
            WHERE device_id = ? ORDER BY sampled_at DESC LIMIT 1
            """,
            (device_id,),
        )
        if row is None:
            return None
        return StorageSampleRow(
            total_gb=row["total_gb"],
            remain_gb=row["remain_gb"],
            formatted=bool(row["formatted"]) if row["formatted"] is not None else None,
            mounted=bool(row["mounted"]) if row["mounted"] is not None else None,
            oldest_recording_utc=row["oldest_recording_utc"],
            sampled_at=row["sampled_at"],
        )

    def last_successful_run_started_at(self, device_id: int) -> datetime | None:
        """Drives `scheduled_archive`'s window (see plan: Scheduling): "last
        successful run start, to now". Only `success`/`partial` count, a
        `failed`/`aborted` run's window shouldn't be treated as covered."""
        row = self._fetchone(
            """
            SELECT started_at FROM archive_runs
            WHERE device_id = ? AND outcome IN ('success', 'partial')
            ORDER BY started_at DESC LIMIT 1
            """,
            (device_id,),
        )
        return parse_iso_utc(row["started_at"]) if row else None

    def oldest_unarchived_start_utc(self, device_id: int) -> datetime | None:
        """Feeds the coverage/lag alarm's other half (see plan: Coverage /
        lag alarm): how far behind ReoVault's own backlog is."""
        row = self._fetchone(
            """
            SELECT MIN(start_utc) AS oldest FROM recordings
            WHERE device_id = ? AND state != 'archived'
            """,
            (device_id,),
        )
        return parse_iso_utc(row["oldest"]) if row and row["oldest"] else None

    def sample_for_integrity_scan(
        self, *, device_id: int, bucket_count: int, bucket_index: int
    ) -> list[RecordingRow]:
        """A rotating ~`1/bucket_count` slice of archived rows, selected by
        `id % bucket_count`. Deterministic and stateless: calling this with
        `bucket_index` cycling `0..bucket_count-1` across successive scans
        (see plan: `integrity_scan`, "a rotating sample") eventually covers
        every archived recording, without a persisted cursor."""
        rows = self._fetchall(
            """
            SELECT * FROM recordings
            WHERE device_id = ? AND state = 'archived' AND id % ? = ?
            """,
            (device_id, bucket_count, bucket_index),
        )
        return [self._row_to_recording(r) for r in rows]

    # -- internals ------------------------------------------------------

    @staticmethod
    def _row_to_recording(row: sqlite3.Row) -> RecordingRow:
        return RecordingRow(
            id=row["id"],
            device_id=row["device_id"],
            channel=row["channel"],
            remote_name=row["remote_name"],
            start_utc=row["start_utc"],
            remote_size=row["remote_size"],
            state=row["state"],
            vault_path=row["vault_path"],
            plaintext_sha256=row["plaintext_sha256"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
        )
