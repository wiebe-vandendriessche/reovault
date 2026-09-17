<p align="center">
    <img src="img/reovault.png" alt="reovault" width="400" />
</p>

# ReoVault
Local-first, self-hosted archiving of Reolink camera recordings: pulls recordings off the camera's SD card before the loop-overwrite eats them, verifies them, encrypts them at rest, and stores them durably.

See [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the full design, decisions, and build order.

## Status

Phases 0-5 of the plan are implemented and tested (skeleton, provider layer, database, crypto/vault, archiver, scheduler/health). Verified against a real Reolink doorbell, not just fakes/mocks. Phase 6 (web dashboard beyond `/healthz`) and Phase 7 (Docker packaging) are not built yet.

## Quick start

Prerequisites: [`reolink-cli`](https://github.com/reolink/reolink-cli) installed and a camera registered under an alias (`reolink-cli device add <alias> --host <ip> --user admin`), and its gateway reachable (`reolink-cli gateway start --addr 127.0.0.1:9000 &`). ReoVault never touches the camera password itself, see the plan's Camera credentials section.

```bash
uv sync --dev
cp reovault.example.toml reovault.toml   # set [[devices]] alias/timezone to match reolink-cli's, and storage paths

export REOVAULT_MASTER_PASSPHRASE="a passphrase, not the camera's"
uv run reovault key init                 # creates data/config/master.key (0600)
uv run reovault doctor                   # preflight: config, storage, DB, key. No camera contact.
uv run reovault probe                    # confirms the camera/gateway are reachable

uv run reovault run --from 2026-04-17T00:00:00 --to 2026-04-17T23:59:59   # one-shot archive run (UTC)
uv run reovault status                   # archived count, bytes, SD card status
uv run reovault verify --all             # re-decrypt + re-hash every archived file
uv run reovault export <id> -o clip.mp4  # decrypt one archived recording back out

uv run reovault daemon                   # runs the scheduler + /healthz continuously
```

`reovault --help` and `reovault key --help` list every command.

## Development

```bash
uv sync --dev
uv run pytest                            # unit + integration tests (camera-free, uses a fake provider)
uv run ruff check . && uv run ruff format --check . && uv run mypy reovault
```

Camera-touching work (`reovault probe`/`run` against real hardware, capturing fixtures, contract tests) needs `reolink-cli` installed and a doorbell on the LAN. See the plan's Verification section.
