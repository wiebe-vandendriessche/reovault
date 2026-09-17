# ReoVault — Implementation Plan

> **This document lives in the repository** at `docs/IMPLEMENTATION_PLAN.md`, so it is cloned to the homelab alongside the code and can be revised there as implementation proceeds. Committing it is step 0 of Phase 0. Nothing else is implemented yet.

## Context

The repository is empty: `README.md`, `LICENSE`, `CLAUDE.md`, two logo images, and a Python `.gitignore`. Nothing has been implemented. This plan defines the whole system from scratch.

**Problem.** Reolink cameras record to a microSD card that loop-overwrites. Once the card wraps, footage is gone permanently. There is no local, durable, encrypted archive of what the camera saw.

**Outcome.** A self-hosted daemon that periodically pulls recordings off the camera's SD card, verifies them, encrypts them at rest, and stores them durably — treating network failures, interrupted downloads, duplicate recordings, and restarts as normal conditions, never losing data already archived, and never re-downloading what it already has.

**Scope boundary.** `/mnt/storage` is backed up and replicated by [Pluton](https://github.com/plutonhq/pluton) (self-hosted restic + rclone orchestration). ReoVault therefore implements **no retention policy and no backup destinations**. It does own at-rest encryption — Pluton encrypts its *restic repository*, not the live source directory.

---

## Upstream verification (done before planning)

Checked against the real sources, not memory. Facts that shape the design:

| Finding | Source | Consequence |
|---|---|---|
| `reolink/reolink-cli` is official, LAN-only, **prebuilt proprietary binary**, latest **v0.19.0 (2026-09-08)** | repo + releases API | Pin the version; no source-level auditing possible |
| Releases ship a per-asset `.sha256` (no combined SHA256SUMS) | releases API | Verify binary by its own `.sha256` at image build |
| `vod download --file <name>` returns the device's **native MP4** and is explicitly unchanged across releases | CHANGELOG | Chosen download path |
| `vod download --from/--to` returns **MPEG-TS** + separate `--audio .aac`, needs ffmpeg remux; format changed in 0.17.0, 0.18.0, 0.18.2, 0.19.0 | CHANGELOG | Rejected — unstable, extra dependency |
| Class-based exit codes: `0` ok, `1` input, `2` network (**retryable**), `3` auth, `4` device, `5` protocol | `skills/reolink-cli/SKILL.md` | Drives retry/backoff classification |
| Times are **naive local ISO** `YYYY-MM-DDTHH:MM:SS`, no timezone, no ms; `--since` only `<N>m\|h\|d` | SKILL.md, `references/media.md` | DST hazard — see Time handling |
| `vod search --limit 0` = unlimited (ceiling 100000); response has `scanned` and `truncated` | CHANGELOG 0.12.4 | Always `--limit 0`, assert `truncated` is false |
| Multi-file download uses `application/vnd.reolink.vod-batch` framing, per-file `requested`/`downloaded`/`skipped` | CHANGELOG 0.11.0, 0.19.0 | Not used in v1 — see below |
| Batch download **silently dropped everything past ~the 210th recording** (fixed 0.13.1) | CHANGELOG | Reinforces one-file-per-invocation |
| Historic VOD bugs: `--type` silently ignored, month-boundary searches returned empty, multi-day ranges empty | CHANGELOG | Never filter server-side on `--type`; pin schema with fixtures |
| Credentials: `~/.config/reolink-cli/aliases.toml` (0600), AES-256-GCM `RLENC1:` ciphertext, decrypted by `credentials.key` **beside it**; CLI refuses group/world-readable files | SECURITY.md, SKILL.md | Delegate credential storage; never store the camera password in ReoVault |
| `--password` on argv is forbidden; use `--password-stdin`, `REOLINK_PASSWORD`, or `--camera <alias>` | SKILL.md | ReoVault only ever passes `--camera <alias>` |
| "The CLI is the supported surface" for automation; gateway HTTP exists for UIs; gateway tokens expire on long pauses | `references/gateway-http.md`, troubleshooting | Use subprocess CLI, not the gateway or MCP |
| CLI is **read-only** against the SD card (cannot format, cannot delete); loop-overwrite is normal | `references/troubleshooting.md`, `storage.md` | Archive-only is the only option; hence the lag alarm |
| `storage` returns `number`, `totalGB`, `remainGB`, `formatted`, `mounted` | `references/storage.md` | Feed the dashboard and the coverage alarm |
| Large downloads may time out; use explicit `--timeout-secs` | troubleshooting | Per-command timeouts, generous for downloads |

**Local environment:** `reolink-cli` is **not installed**; `ffmpeg`/`ffprobe` are **not installed**; Python 3.12.3 present; `/mnt/storage` does **not exist** on this WSL machine.

### Unverified — must be confirmed against the real device early

1. The **exact JSON schema of `vod search`**. Public docs name the fields loosely ("file name, start/end time, size, type, stream") but publish no schema. Task 1.3 below is a probe that captures the real payload into a fixture.
2. Whether `-o FILE` (SKILL.md) or `--file NAME --directory DIR` (CHANGELOG 0.14.1/0.16.1) is the current download syntax — the two docs disagree. Resolve with `reolink-cli vod download --help`.
3. **"D350W"** as a model string is not confirmed by public Reolink sources (searches surface D340W and "Video Doorbell WiFi"). Confirm from the device itself via `reolink-cli device info`. Does not change the architecture.

---

## Decisions (confirmed with the user)

| Area | Decision |
|---|---|
| Fleet | Single Reolink doorbell, SD card, single channel. Data model stays multi-device/multi-channel capable; only one device tested. |
| Download mode | **By-name MP4 only.** `vod search` to enumerate, one `vod download` per recording. No ffmpeg, no remux. |
| Encryption | **Per-file envelope encryption**, AES-256-GCM in fixed frames, per-file DEK wrapped by a master key. |
| Master key | Random 32-byte key file (0600, outside the vault), wrapped by a passphrase from env/Docker secret via Argon2id. Refuse to start on loose permissions. |
| Camera lifecycle | **Read-only.** Never delete from the camera. Add a coverage/lag alarm that warns before the loop-overwrite horizon eats unarchived footage. |
| Dedup + verify | Key `(device_id, channel, remote_name, start_utc)` + SHA-256 of plaintext. Verify size *and* hash before finalize. No ffprobe in v1. |
| Scheduling | Periodic poll with overlap window; **daily at 05:00** by default, configurable. Manual runs. Periodic deeper backfill/reconciliation. |
| Dashboard | Status + playback + manual controls. Localhost-bound, single-user login. Minimal v1; **no retention or backup-destination features** (Pluton's job). |
| Runtime | Python 3.12+, uv, SQLite, Docker Compose, pinned `reolink-cli` baked into the image. |

---

## Architecture

Four layers with one-way dependencies. Camera specifics exist only in the provider layer.

```
            ┌──────────────────────────────────────────────┐
  cron/UI → │  Scheduler  ── ArchiveRun ──► Archiver        │  orchestration
            └───────┬───────────────────────────┬──────────┘
                    │                           │
        ┌───────────▼──────────┐    ┌───────────▼──────────┐
        │ CameraProvider (ABC) │    │ VaultStore (ABC)     │  boundaries
        │  list_recordings()   │    │  put() get() verify()│
        │  fetch(rec, dest)    │    │  delete() open_range()│
        │  storage_status()    │    └───────────┬──────────┘
        └───────────┬──────────┘                │
     ReolinkCliProvider / FakeProvider   EncryptedFsVault
                    │                           │
            subprocess reolink-cli        /vault on /mnt/storage
                    │                           │
        ┌───────────▼───────────────────────────▼──────────┐
        │  Repository (SQLite, WAL)  — single source of truth│
        └───────────────────────────────────────────────────┘
                              ▲
                  FastAPI dashboard (read + trigger)
```

**Why a provider ABC:** `CLAUDE.md` requires camera-specific logic isolated behind an adapter. It also makes the entire archiver testable without a camera, which matters because the one camera is a production doorbell.

**Why subprocess over the gateway/MCP:** upstream states the CLI is the supported automation surface; the gateway's tokens expire over long pauses and add a second long-running process. One process per operation, with the CLI's own exit codes as the error taxonomy.

### Repository layout

```
reovault/
  __main__.py            # `python -m reovault` / console script
  config.py              # pydantic-settings; TOML + env overrides
  models.py              # domain dataclasses (Recording, ArchiveRun, ...)
  db/
    schema.sql           # DDL, applied by a tiny numbered migration runner
    repository.py        # all SQL; the only module that touches sqlite3
  providers/
    base.py              # CameraProvider ABC + provider exceptions
    reolink_cli.py       # subprocess wrapper, JSON parsing, exit-code mapping
    fake.py              # in-memory provider for tests
  crypto/
    envelope.py          # frame format, encrypt/decrypt streams, range reads
    keyring.py           # master key load/unwrap, Argon2id, permission checks
  storage/
    vault.py             # EncryptedFsVault: staging → verify → atomic finalize
  archiver.py            # the state machine; the core of the system
  scheduler.py           # APScheduler jobs, single-instance lock
  health.py              # coverage/lag alarm, storage status, run summaries
  web/
    app.py  routes.py  auth.py  templates/  static/
  cli.py                 # typer: run, backfill, verify, status, key ops, export
tests/
  fixtures/reolink_cli/  # captured real JSON payloads (schema pins)
  unit/  integration/  contract/
Dockerfile  compose.yaml  reovault.example.toml  README.md
```

---

## Data model (SQLite, WAL)

`db/schema.sql`. SQLite is the right call: single-writer, embedded, transactional, trivially backed up by Pluton, no infrastructure.

```sql
CREATE TABLE devices (
  id             INTEGER PRIMARY KEY,
  alias          TEXT NOT NULL UNIQUE,   -- the reolink-cli --camera alias
  channel        INTEGER NOT NULL DEFAULT 0,
  model          TEXT,                   -- from `device info`, informational
  timezone       TEXT NOT NULL,          -- IANA tz the camera's clock runs in
  UNIQUE(alias, channel)
);

CREATE TABLE recordings (
  id             INTEGER PRIMARY KEY,
  device_id      INTEGER NOT NULL REFERENCES devices(id),
  channel        INTEGER NOT NULL,
  remote_name    TEXT    NOT NULL,       -- device filename, e.g. 20260417_120000.mp4
  start_utc      TEXT    NOT NULL,       -- ISO-8601 UTC, normalized on ingest
  end_utc        TEXT,
  duration_s     REAL,
  rec_type       TEXT,                   -- md|people|visitor|package|sched|...
  stream         TEXT,                   -- main|sub
  remote_size    INTEGER,                -- size reported by vod search
  state          TEXT NOT NULL,          -- discovered|downloading|verifying|archived|failed|quarantined
  vault_path     TEXT,                   -- relative path inside the vault
  plaintext_sha256 TEXT,                 -- integrity anchor, set at finalize
  plaintext_size INTEGER,
  ciphertext_size INTEGER,
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  last_error_class TEXT,                 -- network|auth|device|input|protocol|local
  next_attempt_at TEXT,
  first_seen_at  TEXT NOT NULL,
  archived_at    TEXT,
  raw_metadata   TEXT NOT NULL,          -- verbatim vod search JSON object
  UNIQUE(device_id, channel, remote_name, start_utc)   -- the dedup contract
);
CREATE INDEX idx_rec_state_next  ON recordings(state, next_attempt_at);
CREATE INDEX idx_rec_start       ON recordings(device_id, start_utc DESC);
CREATE INDEX idx_rec_hash        ON recordings(plaintext_sha256);

CREATE TABLE archive_runs (
  id INTEGER PRIMARY KEY, device_id INTEGER REFERENCES devices(id),
  trigger TEXT NOT NULL,                 -- schedule|manual|backfill
  window_from_utc TEXT, window_to_utc TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT,                          -- success|partial|failed|aborted
  discovered INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0,
  skipped_dup INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  bytes_archived INTEGER DEFAULT 0, error TEXT
);

CREATE TABLE device_storage_samples (   -- feeds the coverage/lag alarm
  id INTEGER PRIMARY KEY, device_id INTEGER, sampled_at TEXT NOT NULL,
  total_gb REAL, remain_gb REAL, formatted INTEGER, mounted INTEGER,
  oldest_recording_utc TEXT             -- earliest recording still on the card
);
```

`raw_metadata` stores the provider's JSON verbatim. When the upstream schema shifts (and its history says it will), nothing is lost and a backfill can re-derive columns.

**The unique index is the deduplication mechanism.** Overlapping scan windows are handled by `INSERT ... ON CONFLICT DO NOTHING` — the database, not application logic, guarantees a recording is never written twice. `plaintext_sha256` is the second line of defense, catching identical content re-appearing under a new name (e.g. after a card format resets the filename sequence).

---

## Reliability

### The archive state machine

One recording moves through: `discovered → downloading → verifying → archived`, with `failed` (retryable) and `quarantined` (needs a human) as terminal-ish branches.

Ordering is chosen so that **no failure can lose an archived recording and no crash can mark an unfinished one as archived**:

1. `INSERT OR IGNORE` the row as `discovered` — commit. Crash here: rediscovered next run, no duplicate (unique index).
2. Transition to `downloading` — commit. Crash here: staging file orphaned, swept by reconciliation.
3. `vod download --file <name> --directory <staging>` into a per-run staging dir **on the same filesystem as the vault** (required for atomic rename).
4. Verify: downloaded size == `remote_size` from search; stream the file once computing SHA-256.
5. Encrypt staged plaintext → `.tmp` in the vault, `fsync` the file, `fsync` the parent directory.
6. `os.rename()` the `.tmp` into its final vault path — atomic on POSIX.
7. Only now write `state='archived'`, `vault_path`, `plaintext_sha256`, `archived_at` — commit.
8. Delete the staging plaintext.

Crash between 6 and 7 leaves an unreferenced vault file; reconciliation finds it, matches it by hash, and either adopts or deletes it. The invariant holds: **a row is `archived` only if a fully-written, verified, fsynced file exists at `vault_path`.**

### Failure classification and backoff

Map `reolink-cli` exit codes onto policy directly:

| Exit | Class | Policy |
|---|---|---|
| 2 | network | Retry: exponential backoff 1m → 2m → 4m … capped at 6h, ±25% jitter, max 8 attempts, then `failed` (still retried by the next scheduled run) |
| 3 | auth | **Stop the run immediately**, alarm on the dashboard. Retrying a bad password can lock the device account. |
| 4 | device | Retry twice slowly, then `quarantined` + alarm. Likely firmware/unsupported. |
| 1, 5 | input / protocol | `quarantined` immediately + alarm. Indicates a ReoVault bug or an upstream schema change — never retry-loop on it. |
| — | local (disk full, permissions, crypto) | Abort the run, alarm. Never mutate recording state on a storage failure. |

Additional guards, all grounded in documented upstream behavior:
- **Per-command timeouts** via `--timeout-secs`, generous for downloads (default 300s, configurable), short for search/status (30s).
- **Single-instance lock** (`fcntl.flock` on a lockfile) so a manual run can never race the scheduled one.
- **Bounded concurrency**, default 1 download at a time. The doorbell is a small embedded device; parallel downloads risk session exhaustion for no benefit.
- **`truncated`/`scanned` assertions** on every search: if `truncated` is true, log an alarm rather than silently archiving a partial listing.
- **Startup preflight**: assert `reolink-cli --version` equals the pinned version, the DB migrates cleanly, the master key unwraps, the vault is writable, and staging and vault share a filesystem (`os.stat().st_dev`). Fail loudly at start, not at 05:00.

### Coverage / lag alarm

Each run records a `device_storage_samples` row (from `storage` plus the earliest recording seen in a full search). The dashboard alarms when the oldest **unarchived** recording is within a configurable margin (default 20%) of the oldest recording still on the card — i.e. the card is about to overwrite footage ReoVault has not yet saved. This is the real failure mode of an archive-only design, so it gets a first-class indicator rather than a log line.

### Time handling (a genuine correctness hazard)

`reolink-cli` accepts and returns **naive local ISO** timestamps with no timezone. DST transitions make local times ambiguous (repeated hour) or nonexistent (skipped hour).

- Store **UTC only** in the database.
- Convert to camera-local naive ISO at the provider boundary using the device's configured IANA `timezone`.
- Widen every search window by ±1 hour beyond what is strictly needed, so an ambiguous or skipped local hour can never carve a hole in coverage. Duplicates from widening cost nothing — the unique index absorbs them.
- Never pass `--since` for scheduled work (minute granularity, relative to the device clock); always use explicit `--from`/`--to`.
- Log device clock skew (device time vs. host time) each run; large skew is an alarm.

---

## Encryption

### Format

Per-file envelope, AES-256-GCM in fixed 1 MiB frames — chunked rather than one-shot so that (a) memory stays bounded for hour-long recordings and (b) the dashboard can seek without decrypting the whole file.

```
header (plaintext, versioned):
  magic "REOVAULT1" | format_version | frame_size | file_uuid | wrapped_dek | dek_nonce
frames:
  [nonce_0][ct_0][tag_0] [nonce_1][ct_1][tag_1] ... [nonce_n][ct_n][tag_n]
```

- Random 32-byte **DEK per file**, wrapped by the master key with AES-256-GCM. Never reuse a DEK.
- Each frame has its own random nonce, and **AAD = `file_uuid ‖ frame_index ‖ is_final`**. This is what makes truncation, frame reordering, and cross-file frame splicing detectable — a plain per-frame GCM tag alone does not.
- Header is authenticated as AAD of frame 0, so header tampering fails decryption.
- Sidecar `.json` next to the ciphertext: plaintext SHA-256, plaintext size, ciphertext size, algorithm, format version, created-at. Redundant with the DB on purpose — the vault stays self-describing if the DB is ever lost, which is exactly the scenario Pluton restores into.
- Library: `cryptography` (AESGCM). No hand-rolled primitives.

### Key management

```
REOVAULT_MASTER_PASSPHRASE (env var or Docker secret)
        │ Argon2id (params recorded in the key file header)
        ▼
  unwraps  <config>/master.key   (0600, 32 random bytes, outside the vault)
        │
        ▼  wraps each file's DEK
```

- Refuse to start if `master.key` is group- or world-readable — the same guard `reolink-cli` applies to `credentials.key`.
- The key file must live **outside `/mnt/storage`**, or Pluton would replicate it to the same destinations as the ciphertext, collapsing the security boundary. Enforce this with a startup check and document it loudly.
- `reovault key verify` — confirms the passphrase unwraps the key and decrypts a canary.
- `reovault key backup` — emits the key file plus instructions; the README states plainly that losing both key file and passphrase makes the archive unrecoverable.
- Rotation is explicitly **out of scope for v1** (it means rewrapping every DEK); the format carries a version field so it can be added without a migration.

### Vault layout

```
/vault/<device-alias>/<YYYY>/<MM>/<DD>/<start>_<remote-name>.mp4.enc
                                       <start>_<remote-name>.mp4.enc.json
/staging/<run-id>/...                  # same filesystem as /vault
```

Date-sharded so directories stay small and a Pluton/restic scan stays incremental. Only ever written by atomic rename, so Pluton can never capture a torn file.

### Camera credentials

ReoVault **never stores the camera password.** It only passes `--camera <alias>`; `reolink-cli` holds the AES-256-GCM-encrypted credential in `aliases.toml` beside `credentials.key`. The two are mounted read-only into the container as an inseparable pair, and ReoVault's preflight verifies both exist with 0600 permissions. Nothing secret enters the repository, and no password ever appears on a command line or in a log.

---

## Scheduling

APScheduler inside the daemon — one dependency, and it lets the dashboard show and trigger jobs, which an external cron cannot.

| Job | Default | Window | Purpose |
|---|---|---|---|
| `scheduled_archive` | daily 05:00 (cron expression, configurable) | last successful run start − **48h overlap**, to now | Normal operation; the overlap absorbs downtime, network outages, and DST ambiguity |
| `deep_backfill` | weekly, Sunday 04:00 | trailing 30 days (configurable) | Catches anything a narrow window missed and anything the device published late |
| `reconcile` | every 6h | — | Sweeps orphaned staging files, adopts/deletes unreferenced vault files, re-queues `failed` rows whose `next_attempt_at` has passed |
| `integrity_scan` | weekly | — | Re-reads a rotating sample (default 5%) of archived files, re-verifying GCM tags and SHA-256, so bit rot surfaces while Pluton still holds a good copy |
| `storage_sample` | each run | — | Records SD status and feeds the coverage alarm |
| manual | dashboard / `reovault run` | arbitrary | Same code path, `trigger='manual'` |

All jobs are `max_instances=1`, `coalesce=True`, and take the process-wide lock. A missed run due to downtime is *not* fired repeatedly on startup; the overlap window makes catch-up automatic on the next fire.

---

## Dashboard

FastAPI + Jinja2 + HTMX, server-rendered. No SPA, no build step, no Node in the image — appropriate for a handful of pages and consistent with "avoid unnecessary infrastructure."

**Pages**
- **Overview** — last run outcome, next run time, coverage/lag alarm, SD status (`totalGB`/`remainGB`/`mounted`), archived count and bytes, failure count.
- **Recordings** — filter by date/type/state; per-row state, size, hash prefix; inline HTML5 `<video>` playback.
- **Runs** — history with per-run counters and errors.
- **Problems** — `failed` and `quarantined` rows with the error class and message, and a retry action.

**Controls (v1, deliberately small):** trigger archive run, trigger backfill for a date range, retry one recording, re-verify one recording's hash.

**Playback of encrypted files.** `GET /recordings/{id}/stream` honors HTTP `Range`. Because frames are fixed-size, a byte range maps arithmetically to a frame range: decrypt only the frames covering the request and slice. Seeking in a two-hour recording never decrypts more than the frames touched. Decrypted bytes are streamed, never written to disk.

**Security posture.** Binds `127.0.0.1` by default (matching the `reolink-cli` gateway's own default). Single-user login, password hashed with Argon2id, signed session cookie (`HttpOnly`, `SameSite=Strict`), CSRF token on every mutating form, rate-limited login. `0.0.0.0` requires an explicit opt-in flag that logs a warning. TLS/reverse proxy is **documented, not implemented**.

Explicitly **not** in v1 (Pluton's domain or later): retention rules, pruning, backup destinations, cloud replication, thumbnails (would pull in ffmpeg), multi-user accounts, key rotation.

---

## Testing

The single camera is a live doorbell, so almost everything must be testable without it.

1. **`FakeProvider`** — in-memory `CameraProvider` that can be scripted to fail: timeouts, truncated downloads, size mismatches, duplicate names, clock skew, empty results. Every reliability behavior is tested through it.
2. **Fixture-pinned CLI parsing** — `tests/fixtures/reolink_cli/*.json` holds **real captured payloads** from the device (see Verification step 1). `ReolinkCliProvider` parsing tests run against these. If a CLI upgrade changes the schema, these tests fail loudly — the intended alarm, given the VOD surface's history.
3. **Crash-injection integration tests** — drive the archiver with a fault injector that raises at each state-machine boundary, then assert the invariant after recovery: *no row is `archived` without a complete, hash-matching file, and no recording is ever stored twice.* This is the most valuable test in the suite.
4. **Dedup tests** — overlapping windows, re-runs, restarts mid-download, same content under a different `remote_name`. Assert exactly one vault file and one row.
5. **Crypto tests** — round-trip of empty/1-byte/multi-GiB-simulated files; tamper tests (flip a ciphertext byte, truncate the last frame, reorder two frames, swap a frame between files, corrupt the header) must each raise `InvalidTag`, not return plaintext. Property-based (Hypothesis) test: for random offsets/lengths, a range read equals the same slice of the plaintext.
6. **Key management tests** — wrong passphrase, missing key file, 0644 key file (must refuse to start), key file inside the vault (must refuse to start).
7. **Web tests** — auth required on every route, CSRF enforced, `Range` requests return correct bytes and `206`, no plaintext path ever leaks.
8. **Contract tests** (`-m contract`, opt-in, excluded from CI) — run read-only commands against the real doorbell to confirm the fixtures still reflect reality; refresh fixtures from their output.

Tooling: `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy --strict` on `crypto/` and `providers/`. GitHub Actions runs lint + type + unit + integration; contract tests are manual.

---

## Deployment

**Image.** Multi-stage. Builder downloads the pinned `reolink-cli` release archive **and its `.sha256`**, verifies, extracts. Runtime is `python:3.12-slim` with `uv`-installed deps, running as a non-root user. The CLI version is a build arg (`REOLINK_CLI_VERSION=0.19.0`) and the daemon asserts it at startup, satisfying "pin and validate external tool versions when behavior affects correctness."

**compose.yaml volumes**

| Mount | Mode | Notes |
|---|---|---|
| `/config` | rw | `reovault.toml`, SQLite DB, `master.key` — **not** under `/mnt/storage` |
| `/mnt/storage/reovault/vault` | rw | ciphertext + sidecars; Pluton's backup source |
| `/mnt/storage/reovault/staging` | rw | **must share a filesystem with the vault** (atomic rename); asserted at startup |
| `~/.config/reolink-cli` | ro | `aliases.toml` + `credentials.key` pair |

`REOVAULT_MASTER_PASSPHRASE` via Docker secret or an env file outside the repo. Port `127.0.0.1:8080:8080`. `restart: unless-stopped`. Healthcheck hits `/healthz`. Structured JSON logs to stdout.

**Note:** `/mnt/storage` does not exist on this WSL dev machine, so all paths are configuration, never constants, and the dev default points at a local directory.

---

## Build order

Each phase is independently reviewable and leaves the system working.

- **Phase 0 — Skeleton.** Commit this plan as `docs/IMPLEMENTATION_PLAN.md`. Then `pyproject.toml` (uv), package layout, config loading, structured logging, `pytest`/`ruff`/`mypy` wired, CI. *Done when:* the plan is in the repo, `reovault --help` runs, and CI is green.
- **Phase 1 — Provider.** `CameraProvider` ABC, `ReolinkCliProvider` (subprocess, `--output json`, `--camera`, `--timeout-secs`, exit-code mapping), `FakeProvider`. **Capture real fixtures from the doorbell here** and resolve the two open syntax questions. *Done when:* `reovault probe` prints real recordings and fixtures are committed.
- **Phase 2 — Database.** Schema, migration runner, `Repository` with the unique index and state transitions. *Done when:* dedup and state-machine unit tests pass.
- **Phase 3 — Crypto + vault.** Frame format, keyring, `EncryptedFsVault` with staging→verify→atomic finalize, `reovault key` commands. *Done when:* tamper and range-read property tests pass.
- **Phase 4 — Archiver.** The state machine, retry/backoff classification, reconciliation, single-instance lock, `reovault run` / `backfill` / `verify` / `export`. *Done when:* crash-injection tests pass and a real archive run completes end to end.
- **Phase 5 — Scheduler + health.** APScheduler jobs, coverage/lag alarm, integrity scan, `/healthz`. *Done when:* the daemon runs unattended across a full day including a simulated outage.
- **Phase 6 — Dashboard.** Auth, four pages, range-streaming playback, manual controls. *Done when:* a clip plays and seeks in the browser straight from ciphertext.
- **Phase 7 — Packaging.** Dockerfile with checksum-verified CLI, compose, README with the key-backup procedure and the Pluton boundary.

---

## Verification

All camera-touching steps run **on the homelab host**, where the doorbell is reachable on the LAN and `reolink-cli` is installed — this WSL dev machine has neither, and `reolink-cli` is LAN-only with no cloud relay. Phases 0 and 2–3 are fully testable anywhere; Phase 1 onward wants the homelab. Fixtures captured there are committed so the rest of the suite stays camera-free.

**1. Pin reality first (before writing provider code).** Install the pinned CLI, register the doorbell, and capture ground truth:

```bash
reolink-cli --version                                   # must equal the pinned version
reolink-cli vod --help && reolink-cli vod download --help   # resolves -o vs --file/--directory
reolink-cli device add doorbell --host <ip> --user admin --password-stdin
reolink-cli --camera doorbell device info   --output json | tee tests/fixtures/reolink_cli/device_info.json
reolink-cli --camera doorbell storage status --output json | tee tests/fixtures/reolink_cli/storage.json
reolink-cli --camera doorbell vod search --from 2026-09-15T00:00:00 --to 2026-09-15T23:59:59 \
    --limit 0 --output json | tee tests/fixtures/reolink_cli/vod_search.json
```
Confirm the real field names, that `truncated` is false, and the actual model string.

**2. Crypto correctness.** `pytest tests/unit/test_envelope.py -v` — round-trip, all five tamper cases raise `InvalidTag`, Hypothesis range-read property holds.

**3. Reliability under failure.** `pytest tests/integration/test_crash_recovery.py -v` — kill at each boundary; after recovery assert no `archived` row lacks a verified file and no recording is stored twice.

**4. Dedup under overlap.** Run `reovault run` three times over overlapping windows; assert row count, vault file count, and `skipped_dup` counters, and that mtimes of existing vault files are unchanged (nothing rewritten).

**5. End-to-end against the real doorbell.**
```bash
reovault run --device doorbell --from 2026-09-15T00:00:00 --to 2026-09-15T23:59:59
reovault status                      # archived count, bytes, coverage margin
reovault verify --all                # re-decrypt + re-hash every archived file
reovault export <id> -o /tmp/out.mp4 && ffprobe /tmp/out.mp4   # if ffmpeg available
```
Then pull the network cable mid-download and confirm: exit class `network`, a retry scheduled with backoff, no partial file in the vault, and a clean completion on the next run.

**6. Dashboard.** `docker compose up -d`, open `http://127.0.0.1:8080`, confirm login is required, a clip plays and **seeks** (verify `206` responses in devtools), and the manual run button produces a new `archive_runs` row.

**7. Restart safety.** `docker compose restart` mid-run; confirm the lock is released, staging is swept by reconciliation, and no data is lost or duplicated.

**8. Pluton interop.** Confirm the vault contains only fully-written files (no `.tmp` at rest), that `master.key` is **not** inside the Pluton-backed path, and that a restic snapshot of the vault restores into a directory `reovault verify` accepts.

---

## Risks and assumptions

- **`vod search` schema is unverified.** Mitigated by fixture-pinned parsing tests, `raw_metadata` preservation, and treating exit code `5` (protocol) as immediate quarantine rather than a retry loop.
- **`reolink-cli` is a proprietary prebuilt binary** on a fast-moving release train with a documented history of silent VOD bugs. Mitigated by version pinning, checksum verification, a startup version assertion, and never auto-updating.
- **Archive-only means the loop-overwrite window is a hard deadline.** A daily 05:00 cadence must outpace the card wrapping. The coverage/lag alarm exists precisely to make this visible; if it fires regularly, the interval needs shortening.
- **Server-side `--type` filtering is not trusted** (it was silently ignored before 0.17.0). ReoVault enumerates everything and filters locally.
- **The model string "D350W" is unconfirmed** publicly; resolved by `device info` in Verification step 1. No architectural impact.
- **NVR / Home Hub channels are modeled but untested**, and upstream reports the by-name download probe being rejected by an NVR with a 400 (0.18.1). Adding an NVR later will need its own verification pass.
