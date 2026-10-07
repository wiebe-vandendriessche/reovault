# Installation

ReoVault is deployed with Docker Compose, and only with Docker Compose. One
image holds everything: the archiver and scheduler, the web API, the built
dashboard, and a pinned `reolink-cli`. Running from a source checkout without
Docker is a development setup, see [Contributing](contributing.md).

## Docker Compose

ReoVault is published as a signed image for `linux/amd64` and `linux/arm64`
at `ghcr.io/wiebe-vandendriessche/reovault`. You don't need to clone the
repository, two files are enough:

```bash
mkdir reovault && cd reovault
curl -fsSLO https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/reovault.example.toml -o reovault.toml
# edit reovault.toml: your [[devices]], and set [web] email (set-password needs it)

mkdir -p data/config data/vault data/staging data/reolink-cli secrets
echo -n "a passphrase, not the camera's" > secrets/reovault_master_passphrase.txt

docker compose run --rm reovault key init
docker compose up -d
docker compose exec reovault reovault web set-password
```

Create `reovault.toml` and the `data/` folders yourself before the first
`docker compose` command, as above. If Docker has to create a missing bind
mount itself, it makes a root-owned folder, which the container (running as
uid 1000) can't write to, and for `reovault.toml` a folder instead of a file.
If your user isn't uid 1000, run `sudo chown -R 1000:1000 data` once.

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

`./reovault.toml` is mounted read-only (ReoVault never writes its own
config). `./data/{config,vault,staging}` and `./data/reolink-cli`
(reolink-cli's own credential registry, written to when a camera is added
from the Devices page) are bind mounts holding everything that must survive an
upgrade: database, master key, dashboard password, and the archive itself.
`./secrets/reovault_master_passphrase.txt` is a Docker secret file. Never
commit it, and back up `data/config/master.key` together with the passphrase:
losing either makes the archive unrecoverable.

The image bakes in a version-pinned, checksum-verified `reolink-cli` release;
`reovault doctor` asserts it at startup. `reovault daemon` is the container's
entrypoint and supervises the `reolink-gateway` sidecar in-process, so
nothing else needs to run alongside it.

### Upgrading

```bash
docker compose pull && docker compose up -d
```

`:latest` follows the newest release. To upgrade on your own schedule
instead, pin a version in `compose.yaml` (for example
`ghcr.io/wiebe-vandendriessche/reovault:0.4.0`) and bump it after reading the
[release notes](https://github.com/wiebe-vandendriessche/reovault/releases).
Before 1.0 the config schema can still change between releases.

### Verifying the image

Release images are signed keylessly with [cosign](https://docs.sigstore.dev/)
by the release workflow, and carry an attested CycloneDX SBOM:

```bash
cosign verify ghcr.io/wiebe-vandendriessche/reovault:0.4.0 \
  --certificate-identity-regexp '^https://github\.com/wiebe-vandendriessche/reovault/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Each [GitHub Release](https://github.com/wiebe-vandendriessche/reovault/releases)
also has the SBOM (`sbom.cdx.json`) and the vulnerability scan report
(`results.sarif`) attached.

### Building from source

Clone the repository, then in `compose.yaml` comment out the `image:` line
and uncomment the `build:` block below it. `docker compose up -d --build`
builds and starts it; everything else above stays the same.

## Alerts

ReoVault can tell you when archiving needs attention: a failed run, recordings
that failed or were quarantined, a camera falling behind its SD card, or the
camera gateway being down. Channels are configured in `reovault.toml` (or
`REOVAULT_ALERTS__*` env vars), with anything secret in a file, the same way
as the master passphrase:

```toml
[alerts]
ntfy_url = "https://ntfy.sh/your-private-topic"
# ntfy_token_file = "/run/secrets/ntfy_token"
# webhook_url_file = "/run/secrets/alert_webhook"   # Slack/Discord URLs work as-is
# smtp_host = "smtp.example.com"
# smtp_user = "reovault@example.com"
# smtp_password_file = "/run/secrets/smtp_password"
# smtp_to = ["you@example.com"]
```

Mount each secret file with a compose `secrets:` entry (see the commented
example in `compose.yaml`). Which conditions alert, and how often an open
alert reminds you, is set from the dashboard under Settings, Alerts, which
also has a "Send test" button. See [Configuration](configuration.md#alerts).

## First-run checklist

1. Either `reovault.toml` has a `[[devices]]` entry whose `alias` matches one
   `reolink-cli device add` already registered, or you add the first camera
   from the dashboard's Devices page after startup.
2. `REOVAULT_MASTER_PASSPHRASE` (or `REOVAULT_MASTER_PASSPHRASE_FILE` /
   Docker secret) is set. `reovault key init` needs it.
3. `reovault doctor` passes before anything camera-facing is attempted.
4. `[web] email` is set, and `reovault web set-password` is run before exposing the dashboard.
   Until a password is set, the API answers 503 and the dashboard says so (the healthcheck
   still works).
