-- The verbose, raw form of a failed/quarantined recording's error: stderr,
-- the real subprocess exit code, and reolink-cli's own `error.code`, none
-- of which `last_error` (a short plain-English message, see
-- ProviderError.detail) carries. Shown inside the Problems tab's
-- collapsible so a real failure is diagnosable without a shell.
ALTER TABLE recordings ADD COLUMN last_error_detail TEXT;
