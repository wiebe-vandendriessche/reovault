-- The camera's own configured name (e.g. "Front door" from `info`),
-- distinct from `alias` (ReoVault's own reolink-cli identifier) and `model`
-- (the hardware model string). Backed by the same `device_info` call that
-- fills `model`, so it fills in together with it (see
-- reovault/web/app.py's devices backfill) rather than needing a separate
-- camera round trip.
ALTER TABLE devices ADD COLUMN name TEXT;
