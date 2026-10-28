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
`REOVAULT_<SECTION>__<FIELD>` (e.g. `REOVAULT_BACKUP__KEEP_DAYS=30`).
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
| `repository` | none | Where backups go. Each camera gets `<repository>/<alias>`. A local path (`/backups`), an rclone remote (`rclone:nextcloud:reovault`), or any restic backend (`sftp:`, `s3:`, `rest:`, ...). |
| `password_file` | `$RESTIC_PASSWORD_FILE` | The restic password. The image reads it from the `restic_password` Docker secret. |
| `interval_minutes` | `60` | Time between backup runs. A run that takes longer just continues. |
| `keep_days` | `0` | Delete backups older than this, counted from the newest backup. `0` keeps everything. |

Changing `repository` starts fresh at the new destination: the cards are
backed up there again. Clips backed up to the old destination stay
downloadable from it.

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
| `email` | none | The login name; required before `reovault web set-password`. |
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
