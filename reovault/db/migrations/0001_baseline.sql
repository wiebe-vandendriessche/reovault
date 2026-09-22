-- ReoVault schema.
-- Applied by db.repository's tiny numbered migration runner (schema_migrations).

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id             INTEGER PRIMARY KEY,
  alias          TEXT NOT NULL,          -- the reolink-cli --camera alias
  channel        INTEGER NOT NULL DEFAULT 0,
  model          TEXT,                   -- from `device info`, informational
  timezone       TEXT NOT NULL,          -- IANA tz the camera's clock runs in
  UNIQUE(alias, channel)
);

CREATE TABLE IF NOT EXISTS recordings (
  id             INTEGER PRIMARY KEY,
  device_id      INTEGER NOT NULL REFERENCES devices(id),
  channel        INTEGER NOT NULL,
  remote_name    TEXT    NOT NULL,       -- device filename, e.g. 20260417_120000.mp4
  start_utc      TEXT    NOT NULL,       -- ISO-8601 UTC, normalized on ingest
  end_utc        TEXT,
  duration_s     REAL,
  rec_type       TEXT,                   -- md|people|visitor|package|sched|...
  stream         TEXT,                   -- main|sub
  remote_size    INTEGER,                -- size reported by vod search
  state          TEXT NOT NULL,          -- discovered|downloading|verifying|archived|failed|quarantined
  vault_path     TEXT,                   -- relative path inside the vault
  plaintext_sha256 TEXT,                 -- integrity anchor, set at finalize
  plaintext_size INTEGER,
  ciphertext_size INTEGER,
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  last_error_class TEXT,                 -- network|auth|device|input|protocol|local
  next_attempt_at TEXT,
  first_seen_at  TEXT NOT NULL,
  archived_at    TEXT,
  raw_metadata   TEXT NOT NULL,          -- verbatim vod search JSON object
  UNIQUE(device_id, channel, remote_name, start_utc)   -- the dedup contract
);
CREATE INDEX IF NOT EXISTS idx_rec_state_next  ON recordings(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_rec_start       ON recordings(device_id, start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_rec_hash        ON recordings(plaintext_sha256);

-- Dashboard indexes. Additive only: `Repository.migrate()` replays this
-- whole file with `IF NOT EXISTS` on every startup, so a new index reaches
-- an existing database for free, but a new column would not.

-- Partial: on a healthy system this index is empty. Turns the Problems page
-- from a full-table scan (measured 162ms at 313k rows, the *healthy* case,
-- because SQLite has to scan looking for rows that don't exist) into an
-- index lookup (measured 0.12ms), for ~0MB.
CREATE INDEX IF NOT EXISTS idx_rec_problems ON recordings(device_id, start_utc DESC)
  WHERE state IN ('failed', 'quarantined');

-- Covering: lets the Overview aggregate (count + bytes) and the growth
-- chart run index-only instead of touching the base table, which carries
-- `raw_metadata` (~1KB/row). Measured: 232ms -> 40ms at 313k rows.
CREATE INDEX IF NOT EXISTS idx_rec_archived_size
  ON recordings(device_id, state, archived_at, plaintext_size);

CREATE TABLE IF NOT EXISTS archive_runs (
  id INTEGER PRIMARY KEY, device_id INTEGER REFERENCES devices(id),
  trigger TEXT NOT NULL,                 -- schedule|manual|backfill
  window_from_utc TEXT, window_to_utc TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT,                          -- success|partial|failed|aborted
  discovered INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0,
  skipped_dup INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  bytes_archived INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_device_started ON archive_runs(device_id, started_at DESC);

CREATE TABLE IF NOT EXISTS device_storage_samples (   -- feeds the coverage/lag alarm
  id INTEGER PRIMARY KEY, device_id INTEGER, sampled_at TEXT NOT NULL,
  total_gb REAL, remain_gb REAL, formatted INTEGER, mounted INTEGER,
  oldest_recording_utc TEXT             -- earliest recording still on the card
);
