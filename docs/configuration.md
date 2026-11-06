# Configuration

Everything lives in one file, `reovault.toml`, next to `compose.yaml`
(`/config/reovault.toml` in the container; another path with
`REOVAULT_CONFIG_FILE`). You don't have to write it: the first-run screen,
Settings and Cameras create and fill it. To write it by hand instead, start
from [`reovault.example.toml`](https://github.com/wiebe-vandendriessche/reovault/blob/main/reovault.example.toml).
It holds the camera passwords, so it stays owner-only (0600); ReoVault writes
it that way.

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

What the dashboard may change: cameras, destinations, `[backup]
interval_minutes`, `[alerts.rules]` and the `[web]` session, login-limit and
update-check fields. Everything else changes in the file only: the alert
channels (so a stolen session can't redirect a secret), `[backup]
password_file`, `[storage]`, `[tools]` and the other `[web]` fields. Of those,
`[storage]`, `[tools]` and `[web]` are read at startup: restart after an edit.

## `[[cameras]]`

| Field | Default | |
|---|---|---|
| `id` | required | Names the camera's restic repository (`<repository>/<id>`). Letters, digits, `-` and `_`; `reovault-config` is reserved. Never changes. |
| `name` | id | Shown in the dashboard. |
| `timezone` | required | The IANA zone the camera's clock runs in, e.g. `Europe/Brussels`. |
| `host` | required | The camera's IP address (a `:port` suffix is allowed). Give the camera a DHCP reservation. |
| `user` | `admin` | The camera login. |
| `password` | none | The camera password. Keep `reovault.toml` private (0600); ReoVault writes it that way. |
| `enabled` | `true` | `false` stops backing it up; its backups stay. |
| `demo` | `false` | A pretend camera with a sliding week of tiny clips, for trying ReoVault without hardware. |

## `[backup]`

| Field | Default | |
|---|---|---|
| `password_file` | `$RESTIC_PASSWORD_FILE` | The restic password. The image reads `restic-password` next to `compose.yaml`. |
| `interval_minutes` | `60` | Time between backup runs. A run that takes longer just continues. |

## `[[destinations]]`

Where backups go. Each camera gets its own restic repository,
`<repository>/<id>`, in every destination. One destination is the
**primary**: cameras back up to it first. Every other one receives **copies**
of the new snapshots (`restic copy`), so a dead disk or a lost cloud account
never holds the only copy. Copies run in their own threads, so a slow or
offline copy destination doesn't delay backups, with one exception: a primary
with `max_gb` that is full and can't delete anything (its copies don't have
the old snapshots yet) pauses backups until the copies catch up, rather than
grow past its limit. The Destinations tab in Settings edits all of this, and
connecting an rclone destination there writes the remote into `rclone.conf`
for you.

| Field | Default | |
|---|---|---|
| `name` | required | Shown in the dashboard. Unique, and never changes: ReoVault remembers what is on a destination by this name. |
| `repository` | required | A local path (`/backups`), or an rclone remote: `rclone:<remote>:<folder>`. Remotes the dashboard creates are called `reovault-<name>`, e.g. `rclone:reovault-cloud:reovault`. |
| `primary` | see below | Exactly one enabled destination is primary. With a single destination it is implied. |
| `keep_days` | `0` | Delete backups older than this, counted from the newest backup. `0` keeps everything. Keep the primary's longer than your SD cards hold (about 7 days on a busy 256 GB camera). |
| `max_gb` | `0` | A size limit for this destination, in GB (10^9 bytes, as disks and cloud plans count). At 90% of it the oldest backups go, down to 85%; this is checked after every batch. `0` is no limit. |
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
| `password_file` | `./data/web_password` | The login's password hash (the image uses `/data/web_password`). Never in `reovault.toml`. |
| `password_hash` | none | The hash itself, for setting it by environment variable only (`REOVAULT_WEB__PASSWORD_HASH`). |
| `session_key_path` | `./data/web_session.key` | The key that signs sessions. |
| `update_check` | `true` | Once a day, ask GitHub whether a newer release exists, and say so in the dashboard footer. Nothing else is sent. |
| `cookie_secure` | `true` | `false` for a plain-HTTP direct port. |
| `cookie_samesite` | `lax` | The session cookie's SameSite attribute. |
| `check_origin` | `true` | Refuse changes whose Origin header isn't this site. |
| `forwarded_allow_ips` | `127.0.0.1` | The reverse proxy's address; never `*`. |
| `session_max_age_days` | `30` | |
| `login_max_attempts`, `login_window_secs`, `login_lockout_secs` | `5`, `900`, `300` | Login rate limit. |

## `[storage]`

`data_dir` (database, session key, restic cache) and `staging_dir` (the clips
waiting for upload, at most two batches per camera). The image uses `/data` and `/data/staging`.

## `[tools]`

The `reolink-cli`, `restic` and `rclone` binaries and the versions this
release was verified with, plus `gateway_addr` for reolink-cli's local
gateway. ReoVault refuses to start against a different version.
