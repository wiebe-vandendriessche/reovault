# CLI reference

Run `reovault --help`, `reovault key --help`, or `reovault web --help` for
the authoritative, always-current list. This page groups the same commands
with more context.

Commands that operate on a single camera (`run`, `backfill`, `status`,
`verify`, `export`, `probe`, `reconcile`) accept `--device <alias>` and
default to the sole configured device when there's only one.

## Top-level

| Command | What it does |
|---|---|
| `reovault version` | Print the ReoVault version. |
| `reovault doctor` | Sanity-check config, storage paths, and DB migration. No camera contact. |
| `reovault probe [--device ALIAS]` | Confirm the camera/gateway are reachable. |
| `reovault run --from ISO --to ISO [--device ALIAS]` | Trigger an archive run for an explicit UTC window. |
| `reovault backfill [--days N] [--device ALIAS]` | Trigger a deep backfill over a trailing window (defaults to `schedule.backfill_days`). |
| `reovault status [--device ALIAS]` | Archived count, bytes, and SD card storage status (JSON). |
| `reovault verify (--all \| --id N) [--device ALIAS]` | Re-decrypt and re-hash archived files; exits non-zero if any fail. |
| `reovault export ID -o FILE [--device ALIAS]` | Decrypt one archived recording back to a plaintext file. |
| `reovault reconcile [--device ALIAS]` | Sweep orphaned staging files and adopt/delete unreferenced vault files left by a crash. Also runs automatically every `schedule.reconcile_interval_hours`. |
| `reovault daemon` | Run continuously: the scheduler plus the dashboard, for every enabled device at once. This is the Docker image's entrypoint. |

## `reovault key`

Master key lifecycle. Losing both the key file and its passphrase makes
every archived recording permanently unrecoverable. There is no recovery
path by design.

| Command | What it does |
|---|---|
| `reovault key init` | Generate a new master key, wrapped by a passphrase. Refuses to overwrite an existing key. |
| `reovault key verify` | Confirm a passphrase unwraps the master key. |
| `reovault key backup [--to PATH]` | Print (or copy to `--to`) the key file, plus the standing warning to store it somewhere other than the vault/backup path and record the passphrase separately. |

The passphrase itself comes from `$REOVAULT_MASTER_PASSPHRASE` or
`$REOVAULT_MASTER_PASSPHRASE_FILE` (a Docker secret), never a CLI flag, and
never logged.

## `reovault web`

| Command | What it does |
|---|---|
| `reovault web set-password` | Set (or replace) the dashboard's login password. Hidden prompt with confirmation, written to a 0600 file. Refuses if `web.email` isn't configured. |
