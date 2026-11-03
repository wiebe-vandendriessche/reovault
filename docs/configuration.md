# Configuration

Everything lives in one file, `reovault.toml` (`/config/reovault.toml` in the
container). See [`reovault.example.toml`](https://github.com/wiebe-vandendriessche/reovault/blob/main/reovault.example.toml).

## The file is the source of truth

The dashboard's Cameras and Settings pages write their changes back into the
file, keeping your comments and layout, and edits you make to the file show
up in the dashboard within seconds. A write is validated in full and replaces
the file atomically; the previous version is kept as `reovault.toml.bak`. A
hand edit that doesn't validate keeps ReoVault on its last good settings until
it's fixed.

Any setting can also come from an environment variable,
`REOVAULT_<SECTION>__<FIELD>` (e.g. `REOVAULT_BACKUP__INTERVAL_MINUTES=30`).
Environment variables win over the file and show as locked in the dashboard.

`[storage]`, `[tools]` and the `[web]` bind/cookie/proxy fields are read at
startup: change them in the file, then restart.

## `[[cameras]]`

| Field | Default | |
|---|---|---|
| `alias` | required | The `reolink-cli device add <alias>` name, and the name of the camera's restic repository. Never changes. |
| `name` | alias | Shown in the dashboard. |
| `timezone` | required | The IANA zone the camera's clock runs in, e.g. `Europe/Brussels`. |
| `enabled` | `true` | `false` stops backing it up; its backups stay. |
| `demo` | `false` | A pretend camera with a sliding week of tiny clips, for trying ReoVault without hardware. |

## `[backup]`

| Field | Default | |
|---|---|---|
| `password_file` | `$RESTIC_PASSWORD_FILE` | The restic password. The image reads it from the `restic_password` Docker secret. |
| `interval_minutes` | `60` | Time between backup runs. A run that takes longer just continues. |

## `[[destinations]]`

Where backups go. Each camera gets its own restic repository,
`<repository>/<alias>`, in every destination. One destination is the
**primary**: cameras back up to it first. Every other one receives **copies**
of the new snapshots (`restic copy`), so a dead disk or a lost cloud account
never holds the only copy. Copies run in their own threads: a slow or offline
copy destination never delays a backup. The Destinations tab in Settings edits
all of this and can connect a destination without you touching `rclone.conf`.

| Field | Default | |
|---|---|---|
| `name` | required | Shown in the dashboard. Unique, and never changes: ReoVault remembers what is on a destination by this name. |
| `repository` | required | A local path (`/backups`), or an rclone remote (`rclone:nextcloud:reovault`). |
| `primary` | see below | Exactly one enabled destination is primary. With a single destination it is implied. |
| `keep_days` | `0` | Delete backups older than this, counted from the newest backup. `0` keeps everything. Keep the primary's longer than your SD cards hold (about 7 days on a busy 256 GB camera). |
| `max_gb` | `0` | A size limit for this destination. At 90% of it the oldest backups go, down to 85%. `0` is no limit. |
| `enabled` | `true` | `false` stops using it; its backups stay. |

Retention is chosen by ReoVault, not by `restic forget --keep-within`: the
primary never forgets a snapshot a copy still needs and doesn't have yet, and a
camera's newest snapshot is never forgotten. When the size limit forces a
destination to delete backups younger than `keep_days`, an alert says so. All
destinations share the one restic password.

A copy destination only takes snapshots younger than its own `keep_days`, so a
14-day cloud copy of a 30-day local disk holds the last 14 days.

## `[alerts]`

`ntfy_url`, `ntfy_token_file`, `webhook_url_file`, `smtp_host`, `smtp_port`
(587), `smtp_starttls` (true), `smtp_user`, `smtp_password_file`,
`smtp_from`, `smtp_to`. Every secret is a file path, never the secret itself.

### `[alerts.rules]`

| Field | Default | |
|---|---|---|
| `enabled` | `true` | Turn alerting off entirely. |
| `renotify_hours` | `24` | Remind about an alert that's still open. |
| `notify_on_resolve` | `true` | Say when it's fixed. |

## `[web]`

| Field | Default | |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `8080` | The image sets `0.0.0.0`. |
| `email` | none | The login name. Set by the first-run screen and by Change password. |
| `update_check` | `true` | Once a day, ask GitHub whether a newer release exists, and say so in the dashboard footer. Nothing else is sent. |
| `cookie_secure` | `true` | `false` for a plain-HTTP direct port. |
| `forwarded_allow_ips` | `127.0.0.1` | The reverse proxy's address; never `*`. |
| `session_max_age_days` | `30` | |
| `login_max_attempts`, `login_window_secs`, `login_lockout_secs` | `5`, `900`, `300` | Login rate limit. |

## `[storage]`

`data_dir` (database, session key, restic cache) and `staging_dir` (the one
batch of clips waiting for upload). The image uses `/data` and `/data/staging`.

## `[tools]`

The `reolink-cli`, `restic` and `rclone` binaries and the versions this
release was verified with, plus `gateway_addr` for reolink-cli's local
gateway. ReoVault refuses to start against a different version.
