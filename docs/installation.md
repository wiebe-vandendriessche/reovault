# Installation

## Docker (recommended)

```bash
cp reovault.example.toml reovault.toml   # edit [[devices]] etc.
mkdir -p secrets && echo -n "a passphrase, not the camera's" > secrets/reovault_master_passphrase.txt
docker compose run --rm reovault key init
docker compose up -d
docker compose exec reovault reovault web set-password
```

`compose.yaml` supports two deployment modes. Pick one, both are first-class:

* **Direct port publish** (LAN/VPN access, no reverse proxy). Uncomment
  `ports:` and the two `REOVAULT_WEB__*` lines below it. Plain HTTP has no
  certificate, so the session cookie must not require TLS
  (`REOVAULT_WEB__COOKIE_SECURE=false`) or login silently fails. The
  browser accepts the login redirect but drops a `Secure` cookie sent over
  HTTP, which just bounces you back to `/login`. Restricting exposure
  (firewall, VPN, trusted LAN only) is then on you, same as any other
  self-hosted app you publish a port for.
* **Reverse proxy** (Traefik, Nginx Proxy Manager, Caddy, ...) on a shared
  Docker network, the active default in `compose.yaml`. Set
  `REOVAULT_WEB__FORWARDED_ALLOW_IPS` to the proxy's own address (never
  `"*"`, see [Security](security.md)).

`./data/{config,vault,staging}` and `./data/reolink-cli` (reolink-cli's own
credential registry, written to when a camera is added from the Devices tab)
are bind mounts, gitignored. `./secrets/reovault_master_passphrase.txt` is a
Docker secret file, also gitignored. Never commit it.

The image bakes in a version-pinned, checksum-verified `reolink-cli` release
(`REOLINK_CLI_VERSION` build arg); `reovault doctor` asserts it at startup.
`reovault daemon` is the container's entrypoint and supervises the
`reolink-gateway` sidecar in-process, so nothing else needs to run alongside
it. Released images are published for `linux/amd64` and `linux/arm64`.

## Bare metal / development

Prerequisites: [`reolink-cli`](https://github.com/reolink/reolink-cli)
installed and a camera registered under an alias
(`reolink-cli device add <alias> --host <ip> --user admin`), and its gateway
reachable (`reolink-cli gateway start --addr 127.0.0.1:9000 &`). ReoVault
never touches the camera password itself. See
[Architecture](architecture.md).

```bash
uv sync
cp reovault.example.toml reovault.toml   # set [[devices]] alias/timezone to match reolink-cli's, and storage paths

export REOVAULT_MASTER_PASSPHRASE="a passphrase, not the camera's"
uv run reovault key init                 # creates data/config/master.key (0600)
uv run reovault doctor                   # preflight: config, storage, DB, key. No camera contact.
uv run reovault probe                    # confirms the camera/gateway are reachable

uv run reovault run --from 2026-04-17T00:00:00 --to 2026-04-17T23:59:59   # one-shot archive run (UTC)
uv run reovault status                   # archived count, bytes, SD card status
uv run reovault verify --all             # re-decrypt + re-hash every archived file
uv run reovault export <id> -o clip.mp4  # decrypt one archived recording back out

uv run reovault web set-password         # sets the dashboard login (data/config/web_password, 0600)
uv run reovault daemon                   # scheduler + dashboard: http://127.0.0.1:8080
```

See the [CLI reference](cli-reference.md) for every command, or run
`reovault --help` / `reovault key --help`.

## First-run checklist

1. `reovault.toml` has at least one `[[devices]]` entry with an `alias` that
   matches an alias `reolink-cli device add` already registered.
2. `REOVAULT_MASTER_PASSPHRASE` (or `REOVAULT_MASTER_PASSPHRASE_FILE` /
   Docker secret) is set. `reovault key init` needs it.
3. `reovault doctor` passes before anything camera-facing is attempted.
4. `reovault web set-password` is run before exposing the dashboard.
   Without it, every UI route returns 503 (the healthcheck still works).
