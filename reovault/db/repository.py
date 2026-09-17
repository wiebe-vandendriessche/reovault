"""All SQL lives here — the only module that touches `sqlite3` directly (see
plan: Repository layout). Implements the dedup contract and the archive state
machine's transitions (see plan: Reliability).
"""

from __future__ import annotations

import json
import sqlite3
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


@dataclass(frozen=True, slots=True)
class RecordingRow:
    id: int
    device_id: int
    channel: int
    remote_name: str
    start_utc: str
    state: str
    vault_path: str | None
    plaintext_sha256: str | None
    attempts: int
    next_attempt_at: str | None


class Repository:
    """One instance per SQLite database file. Opens with WAL journaling (single
    writer, embedded, no infra — see plan: Data model) and enforces foreign
    keys, which SQLite otherwise leaves off by default."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
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
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

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

    # -- recordings / dedup ----------------------------------------------

    def discover_recording(
        self, *, device_id: int, channel: int, recording: RemoteRecording
    ) -> tuple[int, bool]:
        """The dedup mechanism: `INSERT OR IGNORE` on the unique
        `(device_id, channel, remote_name, start_utc)` key. Overlapping scan
        windows are absorbed here, not by application-level checking — see
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
        row = self.conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        return self._row_to_recording(row) if row else None

    def find_by_hash(self, plaintext_sha256: str) -> RecordingRow | None:
        row = self.conn.execute(
            "SELECT * FROM recordings WHERE plaintext_sha256 = ? AND state = 'archived'",
            (plaintext_sha256,),
        ).fetchone()
        return self._row_to_recording(row) if row else None

    def due_for_retry(self, *, now: datetime | None = None) -> list[RecordingRow]:
        now_iso = _iso(now) if now else _now()
        rows = self.conn.execute(
            """
            SELECT * FROM recordings
            WHERE state = 'failed' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (now_iso,),
        ).fetchall()
        return [self._row_to_recording(r) for r in rows]

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
        and atomically renamed into place — see plan step 6→7. A row is
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

    # -- internals ------------------------------------------------------

    @staticmethod
    def _row_to_recording(row: sqlite3.Row) -> RecordingRow:
        return RecordingRow(
            id=row["id"],
            device_id=row["device_id"],
            channel=row["channel"],
            remote_name=row["remote_name"],
            start_utc=row["start_utc"],
            state=row["state"],
            vault_path=row["vault_path"],
            plaintext_sha256=row["plaintext_sha256"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
        )
