"""SQLite: the clip index and run history. The only module that touches sqlite3.

restic is the source of truth for what is backed up; this index exists so
the dashboard never has to ask a (possibly remote) repository, and so a clip
that was backed up, lost or expired is never downloaded twice.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE clips (
  id              INTEGER PRIMARY KEY,
  camera          TEXT NOT NULL,          -- the camera's alias
  name            TEXT NOT NULL,          -- the name on the card, e.g. 0120261027221944
  start_utc       TEXT NOT NULL,
  end_utc         TEXT,
  size            INTEGER,                -- bytes, as `vod search` reports them
  rec_type        TEXT,                   -- md,people,vehicle,...
  state           TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'backed_up', 'lost', 'expired')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  last_attempt_at TEXT,
  repository      TEXT,                   -- where it was backed up, set with snapshot_id
  snapshot_id     TEXT,
  path            TEXT,                   -- absolute path inside the snapshot
  backed_up_at    TEXT,
  first_seen_at   TEXT NOT NULL,
  UNIQUE (camera, name, start_utc)
);
CREATE INDEX clips_by_state ON clips (camera, state, start_utc);
CREATE INDEX clips_by_start ON clips (camera, start_utc);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  camera      TEXT NOT NULL,
  trigger     TEXT NOT NULL,              -- schedule | manual
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  outcome     TEXT,                       -- ok | error | stopped
  clips       INTEGER NOT NULL DEFAULT 0,
  bytes       INTEGER NOT NULL DEFAULT 0,
  lost        INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX runs_by_camera ON runs (camera, started_at DESC);

CREATE TABLE alert_state (
  key           TEXT PRIMARY KEY,
  condition     TEXT NOT NULL,
  camera        TEXT,
  title         TEXT NOT NULL,
  message       TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_sent_at  TEXT
);

CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def now_iso() -> str:
    return iso(datetime.now(UTC))


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC [start, end) of one local calendar day: 23 h or 25 h on DST days."""
    zone = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Clip:
    id: int
    camera: str
    name: str
    start_utc: str
    end_utc: str | None
    size: int | None
    rec_type: str | None
    state: str
    attempts: int
    last_error: str | None
    last_attempt_at: str | None
    repository: str | None
    snapshot_id: str | None
    path: str | None
    backed_up_at: str | None


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    camera: str
    trigger: str
    started_at: str
    finished_at: str | None
    outcome: str | None
    clips: int
    bytes: int
    lost: int
    error: str | None


_CLIP_COLS = ", ".join(Clip.__slots__)
_RUN_COLS = ", ".join(Run.__slots__)


class Db:
    """One connection, one lock: worker threads and the web threadpool all
    go through it, which is exactly SQLite's single-writer model."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            # executescript commits first, so the transaction is in the script.
            self.conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = 1;\nCOMMIT;")

    def close(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA optimize")
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def _all(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
            return row

    # -- kv --------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- clips -----------------------------------------------------------

    def upsert_clips(
        self,
        camera: str,
        found: list[tuple[str, datetime, datetime | None, int | None, str | None]],
    ) -> int:
        """`found` is what the card lists: (name, start, end, size, type).
        New clips become pending; a pending clip takes the card's latest
        size and end (it may still have been recording when first seen).
        Returns how many were new."""
        now = now_iso()
        with self.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM clips WHERE camera = ?", (camera,)).fetchone()[
                0
            ]
            c.executemany(
                """
                INSERT INTO clips (camera, name, start_utc, end_utc, size, rec_type, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (camera, name, start_utc) DO UPDATE
                  SET size = excluded.size, end_utc = excluded.end_utc
                  WHERE clips.state = 'pending'
                """,
                [
                    (camera, name, iso(start), iso(end) if end else None, size, rec_type, now)
                    for name, start, end, size, rec_type in found
                ],
            )
            after = c.execute("SELECT COUNT(*) FROM clips WHERE camera = ?", (camera,)).fetchone()[
                0
            ]
        return int(after - before)

    def pending(self, camera: str) -> list[Clip]:
        rows = self._all(
            f"SELECT {_CLIP_COLS} FROM clips WHERE camera = ? AND state = 'pending' "
            "ORDER BY start_utc",
            (camera,),
        )
        return [Clip(**dict(r)) for r in rows]

    def clips_by_name(self, camera: str, names: list[str]) -> dict[str, Clip]:
        if not names:
            return {}
        marks = ",".join("?" * len(names))
        rows = self._all(
            f"SELECT {_CLIP_COLS} FROM clips WHERE camera = ? AND name IN ({marks})",
            (camera, *names),
        )
        return {r["name"]: Clip(**dict(r)) for r in rows}

    def newest_start(self, camera: str) -> datetime | None:
        row = self._one("SELECT MAX(start_utc) AS m FROM clips WHERE camera = ?", (camera,))
        return parse_iso(row["m"]) if row and row["m"] else None

    def has_history(self, camera: str, repository: str) -> bool:
        """Any clip ever backed up for this camera to this repository. A
        repository that then reports "does not exist" is a dropped mount or
        a typo, never something to silently re-create."""
        row = self._one(
            "SELECT 1 FROM clips WHERE camera = ? AND repository = ? LIMIT 1", (camera, repository)
        )
        return row is not None

    def mark_backed_up(
        self, camera: str, repository: str, snapshot_id: str, paths: dict[int, str]
    ) -> None:
        now = now_iso()
        with self.tx() as c:
            c.executemany(
                """
                UPDATE clips SET state = 'backed_up', repository = ?, snapshot_id = ?, path = ?,
                  backed_up_at = ?, last_error = NULL
                WHERE id = ? AND camera = ?
                """,
                [(repository, snapshot_id, path, now, cid, camera) for cid, path in paths.items()],
            )

    def record_failure(self, clip_id: int, error: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE clips SET attempts = attempts + 1, last_error = ?, last_attempt_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (error, now_iso(), clip_id),
            )

    def mark_lost(self, clip_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE clips SET state = 'lost', last_attempt_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (now_iso(), clip_id),
            )

    def expire_missing(self, camera: str, repository: str, existing: set[str]) -> int:
        """Clips whose snapshot `forget` removed become expired: never
        downloaded again, no longer offered for download."""
        rows = self._all(
            "SELECT DISTINCT snapshot_id FROM clips WHERE camera = ? AND repository = ? "
            "AND state = 'backed_up'",
            (camera, repository),
        )
        gone = [r["snapshot_id"] for r in rows if r["snapshot_id"] not in existing]
        if not gone:
            return 0
        marks = ",".join("?" * len(gone))
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE clips SET state = 'expired' WHERE camera = ? AND repository = ? "
                f"AND state = 'backed_up' AND snapshot_id IN ({marks})",
                (camera, repository, *gone),
            )
            return cur.rowcount

    def get_clip(self, clip_id: int) -> Clip | None:
        row = self._one(f"SELECT {_CLIP_COLS} FROM clips WHERE id = ?", (clip_id,))
        return Clip(**dict(row)) if row else None

    def clips_between(self, camera: str, start: datetime, end: datetime) -> list[Clip]:
        rows = self._all(
            f"SELECT {_CLIP_COLS} FROM clips WHERE camera = ? AND start_utc >= ? AND start_utc < ? "
            "ORDER BY start_utc",
            (camera, iso(start), iso(end)),
        )
        return [Clip(**dict(r)) for r in rows]

    def backlog(self, camera: str) -> tuple[int, int, str | None]:
        """(pending count, pending bytes, oldest pending start)."""
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b, MIN(start_utc) AS oldest "
            "FROM clips WHERE camera = ? AND state = 'pending'",
            (camera,),
        )
        assert row is not None
        return int(row["n"]), int(row["b"]), row["oldest"]

    def totals(self, camera: str) -> tuple[int, int]:
        """(backed-up clips, backed-up bytes) for the current index."""
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM clips "
            "WHERE camera = ? AND state = 'backed_up'",
            (camera,),
        )
        assert row is not None
        return int(row["n"]), int(row["b"])

    def lost_since(self, camera: str, since: datetime) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM clips WHERE camera = ? AND state = 'lost' "
            "AND last_attempt_at >= ?",
            (camera, iso(since)),
        )
        return int(row["n"]) if row else 0

    def failing(self, camera: str, min_attempts: int) -> list[Clip]:
        rows = self._all(
            f"SELECT {_CLIP_COLS} FROM clips WHERE camera = ? AND state = 'pending' "
            "AND attempts >= ? ORDER BY start_utc",
            (camera, min_attempts),
        )
        return [Clip(**dict(r)) for r in rows]

    def lost_clips(self, camera: str, since: datetime) -> list[Clip]:
        rows = self._all(
            f"SELECT {_CLIP_COLS} FROM clips WHERE camera = ? AND state = 'lost' "
            "AND last_attempt_at >= ? ORDER BY start_utc DESC",
            (camera, iso(since)),
        )
        return [Clip(**dict(r)) for r in rows]

    def throughput(self, camera: str, runs: int = 10) -> float | None:
        """Bytes per second over the last finished runs that moved data."""
        row = self._one(
            """
            SELECT SUM(bytes) AS b,
                   SUM((julianday(finished_at) - julianday(started_at)) * 86400) AS s
            FROM (SELECT bytes, started_at, finished_at FROM runs
                  WHERE camera = ? AND finished_at IS NOT NULL AND bytes > 0
                  ORDER BY started_at DESC LIMIT ?)
            """,
            (camera, runs),
        )
        if not row or not row["b"] or not row["s"]:
            return None
        return float(row["b"]) / max(float(row["s"]), 1.0)

    def retention_preview(self, keep_days: int) -> tuple[int, int]:
        """(clips, bytes) backed up more than `keep_days` before each
        camera's newest backup: what `forget --keep-within` would drop,
        since restic measures from the newest snapshot."""
        row = self._one(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(c.size), 0) AS b FROM clips c
            JOIN (SELECT camera, MAX(backed_up_at) AS newest FROM clips
                  WHERE state = 'backed_up' GROUP BY camera) n ON n.camera = c.camera
            WHERE c.state = 'backed_up'
              AND julianday(c.backed_up_at) < julianday(n.newest) - ?
            """,
            (keep_days,),
        )
        assert row is not None
        return int(row["n"]), int(row["b"])

    # -- alerts ----------------------------------------------------------

    def list_alerts(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM alert_state ORDER BY first_seen_at")

    def open_alert(
        self,
        *,
        key: str,
        condition: str,
        camera: str | None,
        title: str,
        message: str,
        now: datetime,
    ) -> None:
        """A newly true condition; one already open only gets its text
        refreshed, never its timestamps."""
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO alert_state (key, condition, camera, title, message, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET title = excluded.title, message = excluded.message
                """,
                (key, condition, camera, title, message, iso(now)),
            )

    def mark_alert_sent(self, key: str, now: datetime) -> None:
        with self.tx() as c:
            c.execute("UPDATE alert_state SET last_sent_at = ? WHERE key = ?", (iso(now), key))

    def delete_alert(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM alert_state WHERE key = ?", (key,))

    # -- runs ------------------------------------------------------------

    def start_run(self, camera: str, trigger: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (camera, trigger, started_at) VALUES (?, ?, ?)",
                (camera, trigger, now_iso()),
            )
            return int(cur.lastrowid or 0)

    def update_run(self, run_id: int, *, clips: int, bytes_: int, lost: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET clips = ?, bytes = ?, lost = ? WHERE id = ?",
                (clips, bytes_, lost, run_id),
            )

    def finish_run(self, run_id: int, outcome: str, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, error = ? WHERE id = ?",
                (now_iso(), outcome, error, run_id),
            )

    def runs(self, camera: str | None = None, limit: int = 50) -> list[Run]:
        where, params = ("WHERE camera = ?", (camera,)) if camera else ("", ())
        rows = self._all(
            f"SELECT {_RUN_COLS} FROM runs {where} ORDER BY started_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [Run(**dict(r)) for r in rows]

    def abort_unfinished_runs(self) -> None:
        """A run row still open at startup belongs to a process that died."""
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at = ?, outcome = 'error', "
                "error = 'interrupted by a restart' WHERE finished_at IS NULL",
                (now_iso(),),
            )
