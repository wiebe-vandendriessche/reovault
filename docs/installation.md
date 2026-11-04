# Installation

ReoVault runs as one Docker container next to your cameras. Signed images for
`linux/amd64` and `linux/arm64` are published at
`ghcr.io/wiebe-vandendriessche/reovault`; the image already contains the
pinned `reolink-cli`, `restic` and `rclone`.

## Docker Compose

```bash
mkdir reovault && cd reovault
curl -fsSLO https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/compose.yaml
mkdir -p data
curl -fsSL https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/reovault.example.toml -o reovault.toml
touch rclone.conf && chmod 600 reovault.toml rclone.conf
```

That's the whole layout: `compose.yaml`, `reovault.toml`, `rclone.conf` and
`restic-password` (next step) in one folder, and `data/` for everything
ReoVault writes. Create `data/` before the first `docker compose` command:
otherwise Docker creates it as root and the container (uid 1000) can't write to
it.

1. **The restic password.** It encrypts every backup. Generate one and keep a
   copy in your password manager: without it, nothing can be restored.

   ```bash
   openssl rand -base64 32 | tr -d '\n' > restic-password
   chmod 600 restic-password
   ```

2. **Start it.**

   ```bash
   docker compose up -d
   docker compose logs reovault | grep setup.code
   ```

3. **The first-run screen.** Open the dashboard (port 8080). With no login
   yet, it asks for the one-time **setup code** printed in the log above
   (`XXXX-XXXX`, shown on every start until setup is done), and for the email
   and password you want to sign in with. Nobody who can reach the port but
   can't read the log can take the dashboard over.

   Prefer the terminal? `docker compose run --rm reovault web set-password`
   after setting `[web] email` does the same.

4. **Destinations.** Settings, Destinations, **Connect**:
   * a local disk: mount it in `compose.yaml` (`/mnt/disk/reovault:/backups`),
     then connect the folder `/backups`;
   * Nextcloud, SFTP, S3 and Backblaze B2: fill in the form;
   * OneDrive and Google Drive: run `rclone config` on a computer with a
     browser, make the remote, and paste its `[section]` from `rclone.conf`.

   The first destination is the primary. Connect a second (a cloud account, a
   NAS) and it receives copies of everything: that is 3-2-1. Each one can have
   a keep-days and a size limit (`max_gb`). Editing `config/reovault.toml`
   by hand works the same; see [Configuration](configuration.md).

   Then check with `docker compose exec reovault reovault doctor`: it checks
   the tools, the folders and every camera's repository without changing
   anything.

5. **Recovery.** Settings, Recovery says where the restic password lives and
   offers a recovery sheet (no passwords in it). Store the restic password
   somewhere other than this server, then tick the box: until you do, the
   Overview reminds you. ReoVault also backs up its own config into each
   destination, so a replacement server can start from the footage alone; see
   [Restore without ReoVault](restore-without-reovault.md).

6. **Cameras.** Add them on the dashboard's Cameras page (port 8080): find
   them on your network, enter the camera's user and password and pick its
   timezone. ReoVault logs in once to check, then saves the camera in
   `reovault.toml`. A `demo` camera needs no hardware.

`compose.yaml` supports two ways to reach the dashboard: behind a reverse
proxy (the default, no port published), or a direct port on your LAN/VPN. See
the comments at its top and [Security](security.md#deployment-modes).

## The first run

The first run backs up everything that's on each SD card. A second destination is seeded in the background (the dashboard shows "copying 120 of 430") without slowing the backups. A full 256 GB card
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
`reovault.toml`; secrets go in files next to it (the folder is mounted at `/config`):

```toml
[alerts]
ntfy_url = "https://ntfy.sh/an-unguessable-topic"
# ntfy_token_file = "/config/ntfy-token"
# webhook_url_file = "/config/alert-webhook"   # Slack and Discord URLs work as-is
# smtp_host = "smtp.example.com"
# smtp_user = "reovault@example.com"
# smtp_password_file = "/config/smtp-password"
# smtp_to = ["you@example.com"]
```

Settings, Alerts on the dashboard has a **Send test** button.
