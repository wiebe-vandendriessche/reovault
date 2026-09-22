-- Key/value store for dashboard-editable mutable state. One row per
-- document (e.g. key='schedule'), value is a JSON blob. reovault.toml stays
-- the seed source and immutable infrastructure config; this table is the
-- source of truth once seeded.
CREATE TABLE IF NOT EXISTS app_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
