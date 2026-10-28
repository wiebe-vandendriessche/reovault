# Installation

ReoVault runs as one Docker container next to your cameras. Signed images for
`linux/amd64` and `linux/arm64` are published at
`ghcr.io/wiebe-vandendriessche/reovault`; the image already contains the
pinned `reolink-cli`, `restic` and `rclone`.

## Docker Compose

```bash
mkdir reovault && cd reovault
curl -fsSLO https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/compose.yaml
mkdir -p config/rclone data reolink-cli secrets
curl -fsSL https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/reovault.example.toml -o config/reovault.toml
```

Create the folders before the first `docker compose` command: otherwise
Docker creates them as root and the container (uid 1000) can't write to them.

1. **The restic password.** It encrypts every backup. Generate one and keep a
   copy in your password manager: without it, nothing can be restored.

   ```bash
   openssl rand -base64 32 | tr -d '\n' > secrets/restic_password.txt
   chmod 600 secrets/restic_password.txt
   ```

2. **The destination.** In `config/reovault.toml`, set `[backup] repository`:
   * a local disk: mount it in `compose.yaml` (`/mnt/disk/reovault:/backups`)
     and use `repository = "/backups"`;
   * Nextcloud, OneDrive, Google Drive, S3, SFTP and more: create a remote
     with rclone and point at it, e.g. `repository = "rclone:nextcloud:reovault"`.
     Run `rclone config` once on any machine, then copy the resulting
     `rclone.conf` to `config/rclone/rclone.conf`. The folder must stay
     writable: rclone saves refreshed login tokens back into it.

3. **The dashboard login.** Set `[web] email` in `config/reovault.toml`, then:

   ```bash
   docker compose run --rm reovault web set-password
   ```

4. **Start it and check:**

   ```bash
   docker compose up -d
   docker compose exec reovault reovault doctor
   ```

   `doctor` checks the tools, the folders and every camera's repository
   without changing anything.

5. **Cameras.** Add them on the dashboard's Cameras page (port 8080): find
   them on your network, enter the camera's user and password (they go
   straight into reolink-cli's encrypted registry, never into ReoVault), and
   pick its timezone. A `demo` camera needs no hardware.

`compose.yaml` supports two ways to reach the dashboard: behind a reverse
proxy (the default, no port published), or a direct port on your LAN/VPN. See
the comments at its top and [Security](security.md#deployment-modes).

## The first run

The first run backs up everything that's on each SD card. A full 256 GB card
at about 2 MB/s takes a day or two. ReoVault works through it oldest-first
(the clips the card overwrites next) while new clips still go out within the
hour; the dashboard shows the progress and an estimate. Only one 2 GB batch is
on local disk at any time.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

## Verifying the image

Release images are signed keylessly with [cosign](https://docs.sigstore.dev/)
by the release workflow, and carry an attested CycloneDX SBOM:

```bash
cosign verify ghcr.io/wiebe-vandendriessche/reovault:<version> \
  --certificate-identity-regexp '^https://github\.com/wiebe-vandendriessche/reovault/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Building from source

In `compose.yaml`, comment out `image:` and uncomment `build: .`, then
`docker compose up -d --build`.

## Alerts

Alerts go out when backups keep failing, clips are about to be overwritten
before they're backed up, clips keep failing to download, or the weekly check
finds damage. Configure one or more channels under `[alerts]` in
`config/reovault.toml`; secrets go in files mounted as Docker secrets:

```toml
[alerts]
ntfy_url = "https://ntfy.sh/an-unguessable-topic"
# ntfy_token_file = "/run/secrets/ntfy_token"
# webhook_url_file = "/run/secrets/alert_webhook"   # Slack and Discord URLs work as-is
# smtp_host = "smtp.example.com"
# smtp_user = "reovault@example.com"
# smtp_password_file = "/run/secrets/smtp_password"
# smtp_to = ["you@example.com"]
```

Settings, Alerts on the dashboard has a **Send test** button.
