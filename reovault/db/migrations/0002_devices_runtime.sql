-- Adds the columns the dashboard's Devices tab needs. Additive only: no
-- table rebuild, so this applies to a live database in one transaction with
-- no downtime.
--
-- `host`/`user`/`description`/`model` are informational display copies only;
-- the durable, authoritative record of how to reach a camera (including its
-- credential) stays in reolink-cli's own aliases.toml. Never the password.
ALTER TABLE devices ADD COLUMN host TEXT;
ALTER TABLE devices ADD COLUMN user TEXT;
ALTER TABLE devices ADD COLUMN description TEXT;
ALTER TABLE devices ADD COLUMN created_at TEXT;
ALTER TABLE devices ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
