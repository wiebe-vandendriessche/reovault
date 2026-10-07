-- One row per currently-true alert condition, e.g. key='problems:3'. The
-- notifier diffs what's true now against these rows: a new key opens an
-- alert, a missing one resolves it (and deletes the row). State-based, not
-- event-based, so a restart neither loses nor repeats an alert.
CREATE TABLE IF NOT EXISTS alert_state (
  key           TEXT PRIMARY KEY,
  condition     TEXT NOT NULL,
  device_id     INTEGER,
  title         TEXT NOT NULL,
  message       TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  -- NULL until delivered: still inside its grace period, or every channel
  -- failed (retried on the next evaluation).
  last_sent_at  TEXT
);
