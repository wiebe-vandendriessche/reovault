-- Partial and covering: retention checks the vault-wide archived total
-- after every finalized clip (`archived_ciphertext_total`) and prunes in
-- `start_utc` order (`oldest_archived`). Without this both would walk the
-- base table, which carries `raw_metadata` (~1KB/row), on every clip.
-- `id` is explicit so the index order matches `ORDER BY start_utc, id`.
CREATE INDEX IF NOT EXISTS idx_rec_retention ON recordings(start_utc, id, ciphertext_size)
  WHERE state = 'archived';
