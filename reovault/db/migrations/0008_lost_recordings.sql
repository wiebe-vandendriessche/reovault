-- `state = 'lost'`: never archived, and older than the camera's oldest clip,
-- so the card has overwritten it and a retry cannot succeed. `dismissed_at`
-- is when the user acknowledged it on the Problems page; until then it's an
-- open issue on the Overview.
ALTER TABLE recordings ADD COLUMN dismissed_at TEXT;
