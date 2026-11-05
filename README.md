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
<td width="50%"><img src="img/screenshot-overview.png" alt="Overview: whether every SD card is backed up, and per camera where its footage is: on the SD card, the primary and each copy" /></td>
<td width="50%"><img src="img/screenshot-footage.png" alt="Footage: a month calendar and a day's clips by hour, filterable by detection type, each downloadable from the backup, or a whole time range as one zip" /></td>
</tr>
<tr>
<td width="50%"><img src="img/screenshot-activity.png" alt="Activity: every backup run, plus clips that keep failing or that the card overwrote" /></td>
<td width="50%"><img src="img/screenshot-cameras.png" alt="Cameras: each camera's model, firmware and SD card usage, add from LAN discovery, turn backups on or off" /></td>
</tr>
<tr>
<td width="50%"><img src="img/screenshot-settings-destinations.png" alt="Settings, Destinations: the primary destination and its copies, with keep days and a size limit each" /></td>
<td width="50%"><img src="img/screenshot-settings-alerts.png" alt="Settings, Alerts: configured channels, alerts open right now, and what alerts" /></td>
</tr>
</table>

## How it works

```
camera SD card --reolink-cli--> 2 GB batches on local disk --restic--> primary destination --restic copy--> other destinations
```

* Backs up every hour by default: clips recorded since the last run first, then the oldest ones, which the card overwrites next.
* restic does the storing: encryption, deduplication and integrity checks. Each destination keeps backups for a number of days and/or up to a size limit; a second destination gets copies of everything (3-2-1).
* A clip counts as backed up only after its size matched the card's and restic confirmed a snapshot. Download and upload overlap; when the destination is down, ReoVault stops downloading and the clips wait on the SD card, so local disk never holds more than two batches (about 4.3 GB per camera).
* Alerts over ntfy, a webhook (Slack and Discord URLs work as-is) or email when backups keep failing or clips are about to be overwritten before they're backed up.
* Playback stays in the Reolink app for what's still on the card; ReoVault's dashboard downloads anything that's backed up.

## Docker

Signed images for `linux/amd64` and `linux/arm64` are published at `ghcr.io/wiebe-vandendriessche/reovault`, with pinned, checksum-verified `reolink-cli`, `restic` and `rclone` inside.

```bash
mkdir reovault && cd reovault
curl -fsSLO https://raw.githubusercontent.com/wiebe-vandendriessche/reovault/main/compose.yaml
mkdir data
openssl rand -base64 32 | tr -d '\n' > restic-password && chmod 600 restic-password   # keep a copy elsewhere: it unlocks every backup

docker compose up -d
docker compose logs reovault | grep setup.code      # the one-time code for the first-run screen
```

Open the dashboard (port 8080), enter the setup code and choose your login, connect a destination (a local disk, Nextcloud, OneDrive, SFTP, S3 ...; a second one gets copies) and add your cameras. That's it: ReoVault writes `reovault.toml` and `rclone.conf` next to `compose.yaml` for you, and you can still [edit them by hand](https://wiebe-vandendriessche.github.io/reovault/configuration/).

The folder must be writable by uid 1000 (the container user): create it as a normal user, not with `sudo`. [Installation](https://wiebe-vandendriessche.github.io/reovault/installation/) covers destinations, the first run on a full card, deployment behind a reverse proxy, and verifying the image signature.

## Dashboard

A Svelte single-page app on a typed JSON API, updated live over Server-Sent Events, installable as a PWA, password-protected:

* **Overview**: is every SD card backed up; per camera what's waiting and how long until it catches up, and where its footage is, from what's on the SD card back to everything you've kept (months or years): on the card, on the primary, on each copy; every destination with its size and copy progress.
* **Footage**: a month calendar and the day's clips by hour, filterable by detection type; download one clip, an hour, or any range from one or more cameras as one zip, straight out of restic.
* **Activity**: every backup run with its batches and clips, clips that keep failing, and clips the card overwrote before they could be backed up.
* **Cameras**: each camera's model, firmware and SD card usage; add from LAN discovery (ReoVault logs in once to check), turn backups on or off, edit the name, timezone, address and login, or remove a camera.
* **Settings**: destinations (connect, limits, primary), schedule, recovery, alerts, login; written back into `reovault.toml` with your comments kept.

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
