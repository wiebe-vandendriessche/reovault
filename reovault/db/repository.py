"""All SQL lives here, the only module that touches `sqlite3` directly.
Implements the dedup contract and the archive state machine's transitions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from reovault.models import ErrorClass, RemoteRecording
from reovault.timeline import offset_segments, sqlite_offset_modifier

_ISO = "%Y-%m-%dT%H:%M:%S.%f"

# Explicit column list for every recordings query except the single-row
# detail lookup that needs `raw_metadata`. Deliberately excludes it: it's a
# verbatim JSON blob per row, and dragging it through every retry sweep and
# every dashboard list is hundreds of MB of pointless I/O at 313k rows/year.
# See `get_raw_metadata` below.
_REC_COLS = (
    "id, device_id, channel, remote_name, start_utc, end_utc, duration_s, rec_type, stream, "
    "remote_size, state, vault_path, plaintext_sha256, plaintext_size, ciphertext_size, "
    "attempts, last_error, last_error_class, last_error_detail, next_attempt_at, "
    "first_seen_at, archived_at"
)

_DEVICE_COLS = (
    "id, alias, channel, model, timezone, host, user, description, created_at, enabled, name"
)

# Mirrors schema.sql's `state` column comment. Used to validate any state
# value before it's inlined as a SQL literal (see `problems()`), never to
# validate arbitrary external input.
_VALID_STATES = frozenset(
    {"discovered", "downloading", "verifying", "archived", "failed", "quarantined"}
)


def _now() -> str:
    return datetime.now(UTC).strftime(_ISO)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_ISO)


def parse_iso_utc(s: str) -> datetime:
    """Inverse of `_iso`. Public because the archiver needs to reconstruct a
    `RemoteRecording` from a stored `RecordingRow` for retry processing."""
    return datetime.strptime(s, _ISO).replace(tzinfo=UTC)


def _discover_migrations() -> list[tuple[int, str]]:
    """(version, sql text) for every `NNNN_name.sql` file under
    db/migrations/, sorted by the integer prefix. The prefix is parsed
    strictly (`ValueError` on anything else), which is deliberate: a
    malformed migration filename should fail loudly at startup, not sort
    unpredictably next to the real ones."""
    migrations_dir = resources.files("reovault.db.migrations")
    found: list[tuple[int, str]] = []
    for entry in migrations_dir.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        version = int(entry.name.split("_", 1)[0])
        found.append((version, entry.read_text()))
    return sorted(found, key=lambda pair: pair[0])


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
    end_utc: str | None = None
    duration_s: float | None = None
    rec_type: str | None = None
    stream: str | None = None
    plaintext_size: int | None = None
    ciphertext_size: int | None = None
    last_error: str | None = None
    last_error_class: str | None = None
    last_error_detail: str | None = None
    first_seen_at: str | None = None
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceRow:
    id: int
    alias: str
    channel: int
    model: str | None
    timezone: str
    # Informational display copies only; `host`/`user` are never used to
    # actually reach the camera, and the password is never one of these
    # fields or stored anywhere in this row.
    host: str | None = None
    user: str | None = None
    description: str | None = None
    created_at: str | None = None
    enabled: bool = True
    # The camera's own configured name (e.g. "Front door"), distinct from
    # `alias` and `model`.
    name: str | None = None


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    device_id: int | None
    trigger: str
    window_from_utc: str | None
    window_to_utc: str | None
    started_at: str
    finished_at: str | None
    outcome: str | None
    discovered: int
    downloaded: int
    skipped_dup: int
    failed: int
    bytes_archived: int
    error: str | None


@dataclass(frozen=True, slots=True)
class DayBucketRow:
    day: str  # 'YYYY-MM-DD', device-local
    total: int
    archived: int
    problems: int
    bytes: int


@dataclass(frozen=True, slots=True)
class HourBucketRow:
    hour: int  # 0..23, device-local. On a 25h fall-back day, the repeated
    # local hour folds into one bucket holding both hours' clips.
    total: int
    archived: int
    problems: int
    bytes: int


@dataclass(frozen=True, slots=True)
class TotalsRow:
    archived_count: int
    archived_plaintext_bytes: int
    archived_ciphertext_bytes: int
    discovered_count: int
    in_flight_count: int
    failed_count: int
    quarantined_count: int
    first_start_utc: str | None
    last_start_utc: str | None
    last_archived_at: str | None


@dataclass(frozen=True, slots=True)
class OnCardBytesRow:
    """What the SD card's retention window (from the latest storage
    sample's `oldest_recording_utc`) is known to hold, split by archive
    state. Sizes come from `recordings.remote_size` (what the camera itself
    reported at discovery time via VOD search), never a re-read of the
    card, so a device with no sample yet or an unset `remote_size` on an
    old row both degrade to 0 rather than raising."""

    archived_bytes: int
    pending_bytes: int  # discovered/downloading/verifying/failed/quarantined


@dataclass(frozen=True, slots=True)
class DayBytesRow:
    day: str  # 'YYYY-MM-DD', device-local, attributed to the run's start date
    runs: int
    bytes_archived: int
    downloaded: int


class Repository:
    """One instance per SQLite database file. Opens with WAL journaling
    (single writer, embedded, no infra) and enforces foreign keys, which
    SQLite otherwise leaves off by default.

    `check_same_thread=False` plus an internal lock serializing every access:
    the scheduler (its own background thread) and the `/healthz` web app (its
    own threadpool thread, via Starlette's sync-route dispatch) share one
    `Archiver` and therefore one `Repository`. Python's sqlite3 module
    refuses cross-thread use of a connection by default (raises
    `ProgrammingError`), and even with that check disabled, interleaving
    unrelated statements on one connection from multiple threads without
    synchronization is not safe. The lock makes this Repository what
    SQLite's own single-writer model already assumes: one operation at a
    time, from wherever it's called."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Without this the default is 0: an instant SQLITE_BUSY if the CLI
        # and the daemon start against the same file together, which matters
        # more now that migrate() below opens its own BEGIN IMMEDIATE.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        # SQLite's own recommendation: refresh planner stats before the
        # connection goes away, so query plans stay good as the table grows.
        self.conn.execute("PRAGMA optimize")
        self.conn.close()

    def migrate(self) -> None:
        """Runs every unapplied file under db/migrations/, each wrapped in
        its own transaction.

        `sqlite3.Connection.executescript()` issues an implicit COMMIT
        before it runs, whenever a transaction is already open -- so a
        `conn.execute("BEGIN IMMEDIATE")` followed by a separate
        `executescript()` call would have the implicit COMMIT end that
        transaction before the migration's own statements even start,
        leaving them uncommitted-per-statement instead of atomic. The fix is
        to make BEGIN IMMEDIATE/COMMIT literal text *inside* the one string
        passed to executescript(), so they run as ordinary statements within
        the single script SQLite executes.

        Migrations are additive only (ADD COLUMN, CREATE TABLE, CREATE
        INDEX): SQLite can't toggle `PRAGMA foreign_keys` inside a
        transaction, which is what a real column-rebuild would need, and at
        several hundred thousand rows a silent table copy at startup isn't
        acceptable anyway.
        """
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            int(row["version"])
            for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        for version, sql_text in _discover_migrations():
            if version in applied:
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{sql_text}\n"
                "INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ({version}, '{_now()}');\n"
                "COMMIT;\n"
            )
            # sqlite3_exec() (what executescript() calls) stops at the first
            # error but does *not* roll back the transaction it was in the
            # middle of -- that's left to the caller. Without the explicit
            # ROLLBACK here, a broken migration would leave this connection
            # sitting on an open write transaction forever (relying on GC to
            # eventually close it and release the WAL lock is not
            # acceptable). Rolled back, the version is never recorded and
            # the next open retries it; the exception still propagates out
            # of __init__ to stop the daemon rather than run it against a
            # half-migrated database.
            try:
                self.conn.executescript(script)
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

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
        self,
        *,
        alias: str,
        channel: int,
        timezone: str,
        model: str | None = None,
        host: str | None = None,
        user: str | None = None,
        description: str | None = None,
        name: str | None = None,
    ) -> int:
        """Insert or refresh a device row, called by both TOML-seeding at
        startup and the Devices tab's add-camera flow. `enabled` is set only
        on first insert (default 1) and never touched by an update here, so a
        dashboard-side disable survives every later TOML reseed of the same
        `[[devices]]` entry; `created_at` likewise only stamps on insert."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO devices
                    (alias, channel, model, timezone, host, user, description, created_at,
                     enabled, name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(alias, channel) DO UPDATE SET
                    model = COALESCE(excluded.model, devices.model),
                    timezone = excluded.timezone,
                    host = COALESCE(excluded.host, devices.host),
                    user = COALESCE(excluded.user, devices.user),
                    description = COALESCE(excluded.description, devices.description),
                    name = COALESCE(excluded.name, devices.name)
                """,
                (alias, channel, model, timezone, host, user, description, _now(), name),
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

    def list_devices(self, *, enabled_only: bool = False) -> list[DeviceRow]:
        sql = f"SELECT {_DEVICE_COLS} FROM devices"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [self._row_to_device(row) for row in self._fetchall(sql)]

    def set_device_enabled(self, device_id: int, enabled: bool) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE devices SET enabled = ? WHERE id = ?", (1 if enabled else 0, device_id)
            )

    # -- app_settings: mutable dashboard state --

    def get_setting(self, key: str) -> str | None:
        row = self._fetchone("SELECT value FROM app_settings WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, _now()),
            )

    # -- recordings / dedup ----------------------------------------------

    def discover_recording(
        self, *, device_id: int, channel: int, recording: RemoteRecording
    ) -> tuple[int, bool]:
        """The dedup mechanism: `INSERT OR IGNORE` on the unique
        `(device_id, channel, remote_name, start_utc)` key. Overlapping scan
        windows are absorbed here, not by application-level checking; the
        database guarantees a recording is never written twice. Returns
        `(recording_id, created)`."""
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
        row = self._fetchone(f"SELECT {_REC_COLS} FROM recordings WHERE id = ?", (recording_id,))
        return self._row_to_recording(row) if row else None

    def find_by_hash(self, plaintext_sha256: str) -> RecordingRow | None:
        row = self._fetchone(
            f"SELECT {_REC_COLS} FROM recordings WHERE plaintext_sha256 = ? AND state = 'archived'",
            (plaintext_sha256,),
        )
        return self._row_to_recording(row) if row else None

    def find_archived_by_vault_path(self, vault_path: str) -> RecordingRow | None:
        """Used by reconciliation to tell an already-tracked vault file apart
        from an orphan (a crash between rename and the `finalize_archived`
        commit)."""
        row = self._fetchone(
            f"SELECT {_REC_COLS} FROM recordings WHERE vault_path = ? AND state = 'archived'",
            (vault_path,),
        )
        return self._row_to_recording(row) if row else None

    def pending(self, *, device_id: int, now: datetime | None = None) -> list[RecordingRow]:
        """Everything still needing an attempt: freshly `discovered` rows
        (never attempted, e.g. left behind by a run that crashed before
        reaching them) plus `failed` rows whose backoff has elapsed."""
        now_iso = _iso(now) if now else _now()
        rows = self._fetchall(
            f"""
            SELECT {_REC_COLS} FROM recordings
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
            f"""
            SELECT {_REC_COLS} FROM recordings
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
        and the `finalize_archived` commit."""
        row = self._fetchone(
            f"""
            SELECT {_REC_COLS} FROM recordings
            WHERE device_id = ? AND channel = ? AND remote_name = ? AND start_utc = ?
            """,
            (device_id, channel, remote_name, _iso(start_utc)),
        )
        return self._row_to_recording(row) if row else None

    def stuck_in_progress(self, *, device_id: int) -> list[RecordingRow]:
        """Rows left in `downloading`/`verifying` by a crash mid-run.
        Reconciliation requeues these."""
        rows = self._fetchall(
            f"""
            SELECT {_REC_COLS} FROM recordings
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
    # Invariant: no failure can lose an archived recording, and no crash can
    # mark an unfinished one as archived. Each transition is its own commit.

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
        and atomically renamed into place. A row is `archived` only if that
        already happened; this call never itself writes the file."""
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
        error_detail: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'failed', last_error = ?, last_error_class = ?,
                    last_error_detail = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (
                    error,
                    error_class.value,
                    error_detail,
                    _iso(next_attempt_at) if next_attempt_at else None,
                    recording_id,
                ),
            )

    def mark_quarantined(
        self,
        recording_id: int,
        *,
        error: str,
        error_class: ErrorClass,
        error_detail: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE recordings SET
                    state = 'quarantined', last_error = ?, last_error_class = ?,
                    last_error_detail = ?, next_attempt_at = NULL
                WHERE id = ?
                """,
                (error, error_class.value, error_detail, recording_id),
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
        """Drives `scheduled_archive`'s window: last successful run start, to
        now. Only `success`/`partial` count; a `failed`/`aborted` run's
        window shouldn't be treated as covered."""
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
        """Feeds the coverage/lag alarm: how far behind ReoVault's own
        backlog is."""
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
        eventually covers every archived recording, without a persisted
        cursor."""
        rows = self._fetchall(
            f"""
            SELECT {_REC_COLS} FROM recordings
            WHERE device_id = ? AND state = 'archived' AND id % ? = ?
            """,
            (device_id, bucket_count, bucket_index),
        )
        return [self._row_to_recording(r) for r in rows]

    # -- dashboard --------------------------------------------------------

    def ping(self) -> None:
        """`SELECT 1`. Replaces `/healthz`'s raw `conn.execute`, restoring
        "all SQL lives here"."""
        self._fetchone("SELECT 1")

    def get_device(self, device_id: int) -> DeviceRow | None:
        row = self._fetchone(f"SELECT {_DEVICE_COLS} FROM devices WHERE id = ?", (device_id,))
        return self._row_to_device(row) if row is not None else None

    def get_raw_metadata(self, recording_id: int) -> dict[str, Any] | None:
        """The verbatim vod-search JSON, for the single-recording detail
        page only. Deliberately not part of `_REC_COLS` (see its docstring)."""
        row = self._fetchone("SELECT raw_metadata FROM recordings WHERE id = ?", (recording_id,))
        if row is None:
            return None
        loaded: dict[str, Any] = json.loads(row["raw_metadata"])
        return loaded

    def day_buckets(
        self, *, device_id: int, from_utc: datetime, to_utc: datetime, tz: str
    ) -> list[DayBucketRow]:
        """Local-day counts/bytes over `[from_utc, to_utc)`, e.g. for one
        month's calendar. Splits the range into constant-UTC-offset segments
        (see `reovault.timeline`) and merges by day key, so a DST transition
        never misplaces a day's clips. Bounded by the caller's range plus
        `idx_rec_start`; a month is ~26k rows scanned, <=31 rows out."""
        merged: dict[str, DayBucketRow] = {}
        for seg_start, seg_end, offset_minutes in offset_segments(from_utc, to_utc, tz):
            modifier = sqlite_offset_modifier(offset_minutes)
            rows = self._fetchall(
                """
                SELECT date(start_utc, ?) AS day,
                       COUNT(*) AS total,
                       SUM(state = 'archived') AS archived,
                       SUM(state IN ('failed','quarantined')) AS problems,
                       COALESCE(SUM(CASE WHEN state='archived' THEN plaintext_size END), 0) AS bytes
                FROM recordings
                WHERE device_id = ? AND start_utc >= ? AND start_utc < ?
                GROUP BY day
                """,
                (modifier, device_id, _iso(seg_start), _iso(seg_end)),
            )
            for r in rows:
                prev = merged.get(r["day"])
                merged[r["day"]] = DayBucketRow(
                    day=r["day"],
                    total=(prev.total if prev else 0) + r["total"],
                    archived=(prev.archived if prev else 0) + r["archived"],
                    problems=(prev.problems if prev else 0) + r["problems"],
                    bytes=(prev.bytes if prev else 0) + r["bytes"],
                )
        return sorted(merged.values(), key=lambda row: row.day)

    def hour_buckets(
        self, *, device_id: int, from_utc: datetime, to_utc: datetime, tz: str
    ) -> list[HourBucketRow]:
        """Local-hour counts/bytes over `[from_utc, to_utc)`, meant for one
        local day (`from_utc`/`to_utc` = `timeline.local_day_bounds(day, tz)`).
        On a 25h fall-back day the repeated local hour folds into one bucket
        holding both hours' clips; documented, not a bug."""
        merged: dict[int, HourBucketRow] = {}
        for seg_start, seg_end, offset_minutes in offset_segments(from_utc, to_utc, tz):
            modifier = sqlite_offset_modifier(offset_minutes)
            rows = self._fetchall(
                """
                SELECT CAST(strftime('%H', start_utc, ?) AS INTEGER) AS hour,
                       COUNT(*) AS total,
                       SUM(state = 'archived') AS archived,
                       SUM(state IN ('failed','quarantined')) AS problems,
                       COALESCE(SUM(CASE WHEN state='archived' THEN plaintext_size END), 0) AS bytes
                FROM recordings
                WHERE device_id = ? AND start_utc >= ? AND start_utc < ?
                GROUP BY hour
                """,
                (modifier, device_id, _iso(seg_start), _iso(seg_end)),
            )
            for r in rows:
                hour = int(r["hour"])
                prev = merged.get(hour)
                merged[hour] = HourBucketRow(
                    hour=hour,
                    total=(prev.total if prev else 0) + r["total"],
                    archived=(prev.archived if prev else 0) + r["archived"],
                    problems=(prev.problems if prev else 0) + r["problems"],
                    bytes=(prev.bytes if prev else 0) + r["bytes"],
                )
        return sorted(merged.values(), key=lambda row: row.hour)

    def recordings_in_window(
        self,
        *,
        device_id: int,
        from_utc: datetime,
        to_utc: datetime,
        states: Sequence[str] | None = None,
        rec_type: str | None = None,
        limit: int = 500,
        after: tuple[str, int] | None = None,
    ) -> list[RecordingRow]:
        """Bounded, ascending, keyset-paginated. Always a day window plus a
        hard LIMIT: never an unbounded scan. `rec_type` is a comma-separated
        list of simultaneously-true types on the real camera, so it's
        filtered as a token match, never `LIKE`."""
        clauses = ["device_id = ?", "start_utc >= ?", "start_utc < ?"]
        params: list[object] = [device_id, _iso(from_utc), _iso(to_utc)]
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        if rec_type:
            clauses.append("instr(',' || rec_type || ',', ',' || ? || ',') > 0")
            params.append(rec_type)
        if after is not None:
            clauses.append("(start_utc, id) > (?, ?)")
            params.extend(after)
        params.append(limit)
        rows = self._fetchall(
            f"""
            SELECT {_REC_COLS} FROM recordings
            WHERE {" AND ".join(clauses)}
            ORDER BY start_utc ASC, id ASC LIMIT ?
            """,
            tuple(params),
        )
        return [self._row_to_recording(r) for r in rows]

    def problems(
        self,
        *,
        device_id: int,
        states: Sequence[str] = ("failed", "quarantined"),
        error_class: str | None = None,
        limit: int = 100,
        after: tuple[str, int] | None = None,
    ) -> list[RecordingRow]:
        """Descending, keyset-paginated. Served by the `idx_rec_problems`
        partial index: a healthy device (zero matching rows) answers in
        well under a millisecond instead of scanning the whole table.

        `states` is inlined as SQL literals, not bound parameters. This is
        a measured requirement, not a style choice: SQLite's planner can
        only prove `idx_rec_problems` (`WHERE state IN ('failed',
        'quarantined')`) covers this query when it can see the literal
        values at prepare time. With `state IN (?, ?)` it can't rule out a
        future call passing some other state, so it silently falls back to
        scanning `idx_rec_start` instead: measured 142ms vs 0.3ms for the
        identical query at 313k rows, literals being the only difference.
        Safe to inline because every value is checked against
        `_VALID_STATES` first, never raw user text."""
        for state in states:
            if state not in _VALID_STATES:
                raise ValueError(f"not a recognized recording state: {state!r}")
        state_list_sql = ", ".join(f"'{s}'" for s in states)
        clauses = ["device_id = ?", f"state IN ({state_list_sql})"]
        params: list[object] = [device_id]
        if error_class:
            clauses.append("last_error_class = ?")
            params.append(error_class)
        if after is not None:
            clauses.append("(start_utc, id) < (?, ?)")
            params.extend(after)
        params.append(limit)
        rows = self._fetchall(
            f"""
            SELECT {_REC_COLS} FROM recordings
            WHERE {" AND ".join(clauses)}
            ORDER BY start_utc DESC, id DESC LIMIT ?
            """,
            tuple(params),
        )
        return [self._row_to_recording(r) for r in rows]

    def totals(self, device_id: int) -> TotalsRow:
        """One-pass aggregate for the Overview. The archived count/bytes
        portion is served by the `idx_rec_archived_size` covering index; the
        other state counts still touch the base table, acceptable at a
        personal-archive scale."""
        row = self._fetchone(
            """
            SELECT
              SUM(state = 'archived') AS archived_count,
              COALESCE(SUM(CASE WHEN state='archived' THEN plaintext_size END), 0)
                AS archived_plaintext_bytes,
              COALESCE(SUM(CASE WHEN state='archived' THEN ciphertext_size END), 0)
                AS archived_ciphertext_bytes,
              SUM(state = 'discovered') AS discovered_count,
              SUM(state IN ('downloading', 'verifying')) AS in_flight_count,
              SUM(state = 'failed') AS failed_count,
              SUM(state = 'quarantined') AS quarantined_count,
              MIN(start_utc) AS first_start_utc,
              MAX(start_utc) AS last_start_utc,
              MAX(archived_at) AS last_archived_at
            FROM recordings WHERE device_id = ?
            """,
            (device_id,),
        )
        assert row is not None  # aggregate query always returns exactly one row
        return TotalsRow(
            archived_count=row["archived_count"] or 0,
            archived_plaintext_bytes=row["archived_plaintext_bytes"],
            archived_ciphertext_bytes=row["archived_ciphertext_bytes"],
            discovered_count=row["discovered_count"] or 0,
            in_flight_count=row["in_flight_count"] or 0,
            failed_count=row["failed_count"] or 0,
            quarantined_count=row["quarantined_count"] or 0,
            first_start_utc=row["first_start_utc"],
            last_start_utc=row["last_start_utc"],
            last_archived_at=row["last_archived_at"],
        )

    def on_card_bytes(self, device_id: int, since_utc: datetime | None) -> OnCardBytesRow:
        """Archived vs. not-yet-archived bytes from `since_utc` onward, for
        the device card's stacked SD usage bar. The caller decides what
        "since" means: `_devices_context` in app.py passes the earliest
        recording this device has ever discovered, deliberately not the
        card's actual retention boundary, which needs a full 3650-day `vod
        search` (see health.record_storage_sample) that's too slow and too
        failure-prone against a real camera to serve a page load with.
        `since_utc=None` (nothing ever discovered) returns zeroes. Served by
        `idx_rec_start(device_id, start_utc)`."""
        if since_utc is None:
            return OnCardBytesRow(archived_bytes=0, pending_bytes=0)
        row = self._fetchone(
            """
            SELECT
              COALESCE(SUM(CASE WHEN state = 'archived' THEN remote_size END), 0)
                AS archived_bytes,
              COALESCE(SUM(CASE WHEN state != 'archived' THEN remote_size END), 0)
                AS pending_bytes
            FROM recordings
            WHERE device_id = ? AND start_utc >= ?
            """,
            (device_id, _iso(since_utc)),
        )
        assert row is not None  # aggregate query always returns exactly one row
        return OnCardBytesRow(
            archived_bytes=row["archived_bytes"], pending_bytes=row["pending_bytes"]
        )

    def recent_runs(
        self, *, device_id: int, limit: int = 50, before_id: int | None = None
    ) -> list[RunRow]:
        clauses = ["device_id = ?"]
        params: list[object] = [device_id]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        params.append(limit)
        rows = self._fetchall(
            f"""
            SELECT * FROM archive_runs WHERE {" AND ".join(clauses)}
            ORDER BY id DESC LIMIT ?
            """,
            tuple(params),
        )
        return [self._row_to_run(r) for r in rows]

    def get_run(self, run_id: int) -> RunRow | None:
        row = self._fetchone("SELECT * FROM archive_runs WHERE id = ?", (run_id,))
        return self._row_to_run(row) if row else None

    def last_finished_run(self, device_id: int) -> RunRow | None:
        row = self._fetchone(
            """
            SELECT * FROM archive_runs
            WHERE device_id = ? AND finished_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (device_id,),
        )
        return self._row_to_run(row) if row else None

    def running_run(self, device_id: int) -> RunRow | None:
        """A run currently in flight, written by `start_run()` the moment
        the job begins, before any recording is processed."""
        row = self._fetchone(
            """
            SELECT * FROM archive_runs
            WHERE device_id = ? AND finished_at IS NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (device_id,),
        )
        return self._row_to_run(row) if row else None

    def abort_stale_running_runs(self, device_id: int, *, error: str) -> int:
        """A `finished_at IS NULL` row can only mean one of two things: a run
        genuinely in progress right now, or one whose process died before it
        could call `finish_run` (killed, crashed, `daemon` restarted). Called
        once whenever this device gets a fresh `Archiver` (see
        `fleet.build_archiver`), which is exactly the moment a process can
        state with certainty that no thread of *this* process is running
        that old job: the object that would have finished it did not exist
        until this call. Never called mid-run by the same process. Returns
        the number of rows closed, purely for logging."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE archive_runs SET finished_at = ?, outcome = 'aborted', error = ?
                WHERE device_id = ? AND finished_at IS NULL
                """,
                (_now(), error, device_id),
            )
            return cur.rowcount

    def runs_per_day(
        self, *, device_id: int, from_utc: datetime, to_utc: datetime, tz: str
    ) -> list[DayBytesRow]:
        """The growth series: bytes attributed to the *run's* local start
        date, which is what "vault growth per day" means. `archive_runs` is
        ~1500 rows/year, so this never touches the (much larger) recordings
        table."""
        merged: dict[str, DayBytesRow] = {}
        for seg_start, seg_end, offset_minutes in offset_segments(from_utc, to_utc, tz):
            modifier = sqlite_offset_modifier(offset_minutes)
            rows = self._fetchall(
                """
                SELECT date(started_at, ?) AS day,
                       COUNT(*) AS runs,
                       COALESCE(SUM(bytes_archived), 0) AS bytes_archived,
                       COALESCE(SUM(downloaded), 0) AS downloaded
                FROM archive_runs
                WHERE device_id = ? AND started_at >= ? AND started_at < ?
                GROUP BY day
                """,
                (modifier, device_id, _iso(seg_start), _iso(seg_end)),
            )
            for r in rows:
                prev = merged.get(r["day"])
                merged[r["day"]] = DayBytesRow(
                    day=r["day"],
                    runs=(prev.runs if prev else 0) + r["runs"],
                    bytes_archived=(prev.bytes_archived if prev else 0) + r["bytes_archived"],
                    downloaded=(prev.downloaded if prev else 0) + r["downloaded"],
                )
        return sorted(merged.values(), key=lambda row: row.day)

    def iter_archived(self, *, device_id: int, batch_size: int = 500) -> Iterator[RecordingRow]:
        """Keyset batches on `id`, each its own `_fetchall` call so the lock
        is never held across a `yield` (that would block the archiver, or a
        concurrent web request, for the whole scan). Replaces
        `cli.py verify --all`'s raw SQL, which would otherwise choke on
        313k rows."""
        last_id = 0
        while True:
            rows = self._fetchall(
                f"""
                SELECT {_REC_COLS} FROM recordings
                WHERE device_id = ? AND state = 'archived' AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (device_id, last_id, batch_size),
            )
            if not rows:
                return
            for r in rows:
                last_id = r["id"]
                yield self._row_to_recording(r)

    def retry_recording(self, recording_id: int, *, now: datetime | None = None) -> bool:
        """`failed`/`quarantined` -> `failed`, attempts reset, eligible for
        the very next run's `pending()` sweep. Returns False if the row is
        missing or in another state (e.g. already archived)."""
        now_iso = _iso(now) if now else _now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE recordings SET state = 'failed', attempts = 0, next_attempt_at = ?
                WHERE id = ? AND state IN ('failed', 'quarantined')
                """,
                (now_iso, recording_id),
            )
            return cur.rowcount > 0

    def distinct_rec_types(
        self, *, device_id: int, from_utc: datetime, to_utc: datetime
    ) -> list[str]:
        """The type-chip vocabulary for one day, discovered rather than
        hardcoded: `rec_type` is a comma-separated list of simultaneously
        true types, and the device emits whatever it emits. Hardcoding an
        enum here would bake in an assumption about a set of values that
        isn't guaranteed stable across firmware or camera models."""
        rows = self._fetchall(
            """
            SELECT DISTINCT rec_type FROM recordings
            WHERE device_id = ? AND start_utc >= ? AND start_utc < ? AND rec_type IS NOT NULL
            """,
            (device_id, _iso(from_utc), _iso(to_utc)),
        )
        types: set[str] = set()
        for row in rows:
            types.update(t for t in row["rec_type"].split(",") if t)
        return sorted(types)

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRow:
        return RunRow(
            id=row["id"],
            device_id=row["device_id"],
            trigger=row["trigger"],
            window_from_utc=row["window_from_utc"],
            window_to_utc=row["window_to_utc"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            outcome=row["outcome"],
            discovered=row["discovered"],
            downloaded=row["downloaded"],
            skipped_dup=row["skipped_dup"],
            failed=row["failed"],
            bytes_archived=row["bytes_archived"],
            error=row["error"],
        )

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> DeviceRow:
        return DeviceRow(
            id=row["id"],
            alias=row["alias"],
            channel=row["channel"],
            model=row["model"],
            timezone=row["timezone"],
            host=row["host"],
            user=row["user"],
            description=row["description"],
            created_at=row["created_at"],
            enabled=bool(row["enabled"]),
            name=row["name"],
        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def _row_to_recording(row: sqlite3.Row) -> RecordingRow:
        keys = row.keys()
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
            # These columns are present whenever the query used `_REC_COLS`
            # (every list/lookup query) but absent from the narrower
            # `device_storage_samples`-adjacent queries that never construct
            # a RecordingRow, so this mapper stays total either way.
            end_utc=row["end_utc"] if "end_utc" in keys else None,
            duration_s=row["duration_s"] if "duration_s" in keys else None,
            rec_type=row["rec_type"] if "rec_type" in keys else None,
            stream=row["stream"] if "stream" in keys else None,
            plaintext_size=row["plaintext_size"] if "plaintext_size" in keys else None,
            ciphertext_size=row["ciphertext_size"] if "ciphertext_size" in keys else None,
            last_error=row["last_error"] if "last_error" in keys else None,
            last_error_class=row["last_error_class"] if "last_error_class" in keys else None,
            last_error_detail=row["last_error_detail"] if "last_error_detail" in keys else None,
            first_seen_at=row["first_seen_at"] if "first_seen_at" in keys else None,
            archived_at=row["archived_at"] if "archived_at" in keys else None,
        )
