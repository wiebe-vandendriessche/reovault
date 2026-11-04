<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="img/reovault-bg.svg" />
    <img src="img/reovault-dark-bg.svg" alt="ReoVault" width="400" />
  </picture>
</p>

# ReoVault

[![CI](https://github.com/wiebe-vandendriessche/reovault/actions/workflows/ci.yml/badge.svg)](https://github.com/wiebe-vandendriessche/reovault/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/wiebe-vandendriessche/reovault)](https://github.com/wiebe-vandendriessche/reovault/releases)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Docs](https://github.com/wiebe-vandendriessche/reovault/actions/workflows/docs.yml/badge.svg)](https://wiebe-vandendriessche.github.io/reovault/)

> **Unofficial, independent project.** ReoVault itself is not affiliated with, endorsed by, or sponsored by Reolink. "Reolink" and any related names, logos, or trademarks belong to their respective owners and are used here only to describe device compatibility. ReoVault talks to cameras through Reolink's own official [`reolink-cli`](https://github.com/reolink/reolink-cli) tool (published under the `reolink` GitHub organization), never a reimplementation of Reolink's protocol.

Self-hosted backups of Reolink SD cards: ReoVault copies every clip off each camera's SD card before the loop recording overwrites it, into an encrypted [restic](https://restic.net/) backup on a local disk, Nextcloud, OneDrive, S3 or anything else [rclone](https://rclone.org/) reaches. Your footage stays restorable with plain restic, with or without ReoVault.

Full docs (installation, dashboard guide, configuration reference, architecture): **https://wiebe-vandendriessche.github.io/reovault/**

## Screenshots

<table>
<tr>
<td width="50%"><img src="img/screenshot-overview.png" alt="Overview: whether every SD card is backed up, each camera's card and next backup, and where the backups go" /></td>
<td width="50%"><img src="img/screenshot-footage.png" alt="Footage: a month calendar and a day's clips by hour, filterable by detection type, each downloadable from the backup, or a whole time range as one zip" /></td>
</tr>
<tr>
<td width="50%"><img src="img/screenshot-activity.png" alt="Activity: every backup run, plus clips that keep failing or that the card overwrote" /></td>
<td width="50%"><img src="img/screenshot-cameras.png" alt="Cameras: each camera with its status and backup totals, add from LAN discovery, turn backups on or off" /></td>
</tr>
<tr>
<td width="50%"><img src="img/screenshot-settings-destinations.png" alt="Settings, Destinations: the primary destination and its copies, with keep days and a size limit each" /></td>
<td width="50%"><img src="img/screenshot-settings-alerts.png" alt="Settings, Alerts: configured channels, alerts open right now, and what alerts" /></td>
</tr>
</table>

## How it works

```
camera SD card --reolink-cli--> one 2 GB batch on local disk --restic--> your destination
```

* Backs up every hour by default: clips recorded since the last run first, then the oldest ones, which the card overwrites next.
* restic does the storing: encryption, deduplication, integrity checks and retention ("keep N days", or forever).
* A clip counts as backed up only after its size matched the card's and restic confirmed a snapshot. Download and upload overlap; when the destination is down, ReoVault stops downloading and the clips wait on the SD card, so local disk never holds more than two batches (about 5 GB per camera).
* Alerts over ntfy, a webhook (Slack and Discord URLs work as-is) or email when backups keep failing or clips are about to be overwritten before they're backed up.
* Playback stays in the Reolink app for what's still on the card; ReoVault's dashboard downloads anything that's backed up.

## Docker

Signed images for `linux/amd64` and `linux/arm64` are published at `ghcr.io/wiebe-vandendriessche/reovault`, with pinned, checksum-verified `reolink-cli`, `restic` and `rclone` inside.

```bash
mkdir reovault && cd reovault
curl -fsSLO https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/compose.yaml
mkdir -p data
curl -fsSL https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/reovault.example.toml -o reovault.toml
touch rclone.conf && chmod 600 reovault.toml rclone.conf

openssl rand -base64 32 | tr -d '\n' > restic-password && chmod 600 restic-password   # keep a copy: it unlocks every backup

docker compose up -d
docker compose logs reovault | grep setup.code      # the one-time code for the first-run screen
```

Then open the dashboard (port 8080), finish setup with that code, connect a destination (a local disk, Nextcloud, OneDrive, SFTP, S3 ...; add a second one for 3-2-1 copies) and add your cameras. Create the folders before the first `docker compose` command: otherwise Docker creates them as root and the container (uid 1000) can't write to them. [Installation](https://wiebe-vandendriessche.github.io/reovault/installation/) covers destinations, the first run on a full card, deployment behind a reverse proxy, and verifying the image signature.

## Dashboard

A Svelte single-page app on a typed JSON API, updated live over Server-Sent Events, installable as a PWA, password-protected:

* **Overview**: is every SD card backed up; per camera what's waiting, how long until it catches up, and the card's fill; the destination and retention.
* **Footage**: a month calendar and the day's clips by hour, filterable by detection type; download a clip or a whole day as a zip, straight out of restic.
* **Activity**: every backup run, clips that keep failing, and clips the card overwrote before they could be backed up.
* **Cameras**: add from LAN discovery (ReoVault logs in once to check, then saves the camera in `reovault.toml`), turn backups on or off, edit the name, timezone and login.
* **Settings**: the backup destination (with a test), interval, retention, alerts; written back into `reovault.toml` with your comments kept.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md), including a demo-camera dev setup that needs no hardware.

```bash
uv sync --dev
PATH=$PWD/.tools:$PATH uv run pytest     # the tests run the real pinned restic and rclone, see CONTRIBUTING.md
uv run ruff check . && uv run ruff format --check . && uv run mypy reovault

cd dashboard && npm ci --ignore-scripts
npm run dev                              # the dashboard on :5173, proxying /api to a daemon on :8080
npm run check && npm test && npm run build
```
