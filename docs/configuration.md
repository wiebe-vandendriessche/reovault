# Configuration

ReoVault reads `reovault.toml` (or the path in `$REOVAULT_CONFIG_FILE`,
default `./reovault.toml`), merged with `REOVAULT_*` environment variables.
**Env always wins over the file.** Nested keys use a double underscore:
`REOVAULT_WEB__PORT=9090` sets `[web] port`.

Start from [`reovault.example.toml`](https://github.com/wiebe-vandendriessche/reovault/blob/main/reovault.example.toml).

Nothing in config is a secret by itself. `REOVAULT_MASTER_PASSPHRASE` and the
camera password are deliberately not config fields. See
[Security](security.md).

## `[[devices]]`

One entry per camera. A list, so multi-camera setups just add more entries.

| Field | Type | Default | Notes |
|---|---|---|---|
| `alias` | string | required | Must match a `reolink-cli device add <alias> ...` alias. |
| `channel` | int | `0` | Camera channel (multi-channel NVR-style devices). |
| `timezone` | string | required | IANA tz name the camera's clock runs in, e.g. `America/New_York`. |
| `name` | string | none | Display name shown in the dashboard instead of `alias`; auto-backfilled from the camera's own name once discovered if left unset. |

Devices are seeded from `reovault.toml` on every startup (never deleted or
disabled by a reseed); the dashboard's Devices tab can also add, rename,
enable/disable, and schedule cameras at runtime.

## `[reolink_cli]`

| Field | Type | Default |
|---|---|---|
| `binary` | string | `reolink-cli` |
| `pinned_version` | string | `0.19.0` |
| `gateway_addr` | string | `127.0.0.1:9000` |
| `search_timeout_secs` | int | `30` |
| `download_timeout_secs` | int | `300` |
| `registry_dir` | path | `$HOME/.config/reolink-cli` |

`registry_dir` must match `reolink-cli`'s own actual config directory. It's
used only so `reovault doctor` can preflight that the directory is writable.

## `[storage]`

| Field | Type | Default |
|---|---|---|
| `config_dir` | path | `./data/config` |
| `vault_dir` | path | `./data/vault` |
| `staging_dir` | path | `./data/staging` |
| `db_path` | path | `./data/config/reovault.db` |
| `master_key_path` | path | `./data/config/master.key` |

`staging_dir` and `vault_dir` must share a filesystem (archiving finalizes a
download with an atomic rename); `reovault doctor` asserts this at startup.
`master_key_path` must live outside `vault_dir`, or a downstream backup tool
that treats the vault as its source would replicate the key alongside the
ciphertext it unlocks.

## `[schedule]`

| Field | Type | Default |
|---|---|---|
| `archive_cron` | cron string | `0 5 * * *` |
| `archive_enabled` | bool | `true` |
| `archive_overlap_hours` | int | `48` |
| `backfill_cron` | cron string | `0 4 * * 0` |
| `backfill_enabled` | bool | `true` |
| `backfill_days` | int | `30` |
| `reconcile_interval_hours` | int | `6` |
| `reconcile_enabled` | bool | `true` |
| `integrity_scan_sample_pct` | float | `5.0` |
| `integrity_scan_enabled` | bool | `true` |
| `timezone` | string or unset | unset |

`timezone` unset (the default) means each device's cron jobs evaluate in
that device's own `[[devices]] timezone`. Setting it forces every device
onto one timezone. This table only seeds the dashboard's per-camera schedule
on first boot. Once a camera's schedule has been saved from the Schedule
tab, that camera's own saved schedule wins from then on. Setting any
`REOVAULT_SCHEDULE__*` env var instead pins this schedule for every camera
and takes the dashboard's Schedule tab out of the loop entirely.

## `[web]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `127.0.0.1` | Bind `0.0.0.0` in a container. Loopback there is the container's own namespace, unreachable from a reverse proxy. |
| `port` | int | `8080` | |
| `password_hash` | string or unset | unset | Set via `REOVAULT_WEB__PASSWORD_HASH` (Docker secret) or the `password_file` below. App still starts and `/healthz` still works if unset; every UI route returns 503 until set. |
| `email` | string or unset | unset | Login identity field (password-manager autofill, not an authorization boundary). `reovault web set-password` refuses to run without it. |
| `password_file` | path | `./data/config/web_password` | |
| `session_key_path` | path | `./data/config/web_session.key` | |
| `session_max_age_days` | int | `30` | |
| `cookie_secure` | bool | `true` | Set to `false` only for a direct-port, plain-HTTP deployment. The browser silently drops a `Secure` cookie sent over HTTP, which breaks login. Leave `true` behind a reverse proxy terminating TLS. See [Security](security.md#deployment-modes). |
| `cookie_samesite` | string | `lax` | |
| `check_origin` | bool | `true` | Compares the `Origin` header's netloc against `Host` on mutating requests, as CSRF defense-in-depth. A proxy preserves both headers, so this is safe to leave on in either deployment mode. |
| `login_max_attempts` | int | `5` | |
| `login_window_secs` | int | `900` | |
| `login_lockout_secs` | int | `300` | |
| `forwarded_allow_ips` | string | `127.0.0.1` | Uvicorn only trusts `X-Forwarded-*` from these addresses. Behind a reverse proxy in Docker, set this to the proxy's own container IP/subnet, never `"*"` unless the port is genuinely unreachable except through the proxy. |
| `stream_chunk_bytes` | int | `1048576` | |
| `page_size` | int | `100` | |
