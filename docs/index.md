# ReoVault

!!! note "Unofficial, independent project"
    ReoVault itself is not affiliated with, endorsed by, or sponsored by Reolink. "Reolink" and any related names, logos, or trademarks belong to their respective owners and are used here only to describe device compatibility. ReoVault talks to cameras through Reolink's own official [`reolink-cli`](https://github.com/reolink/reolink-cli) tool (published under the `reolink` GitHub organization), never a reimplementation of Reolink's protocol.

Self-hosted backups of Reolink SD cards. ReoVault copies every clip off each
camera's SD card before the card's loop recording overwrites it, and keeps it
in an encrypted [restic](https://restic.net/) backup on a disk or cloud
account of your choice: a local drive, Nextcloud, OneDrive, S3 and anything
else [rclone](https://rclone.org/) reaches.

## Why

Reolink cameras record to a microSD card that overwrites its oldest footage
once it's full. Whatever happened a week ago is gone, and so is everything if
the camera is stolen. ReoVault keeps a copy that outlives the card.

## What it does

* **Backs up continuously:** every hour by default, newest clips first, then
  the oldest ones the card will overwrite next.
* **Only restic stores your footage:** encrypted, deduplicated, verified,
  with retention. You can restore it with plain restic, with or without
  ReoVault: see [Restore without ReoVault](restore-without-reovault.md).
* **Tells you when something's wrong:** ntfy, webhook or email alerts when
  backups keep failing or clips are about to be overwritten before they're
  backed up.
* **A small dashboard:** is every card backed up, what's waiting, and a
  calendar to download any backed-up clip or day.
* **Light on your server:** only one 2 GB batch of clips is ever on local
  disk.

Playback stays in the Reolink app, for whatever is still on the card.

## Where to go next

* [Installation](installation.md): Docker Compose setup, upgrading, and alerts
* [Compatible devices](compatibility.md): camera models verified so far
* [Dashboard](dashboard.md): what each page shows and does
* [Configuration](configuration.md): every `reovault.toml` / env var option
* [CLI reference](cli-reference.md): every `reovault` subcommand
* [Restore without ReoVault](restore-without-reovault.md): plain restic recipes
* [Architecture](architecture.md): how a backup run works
* [Security](security.md): the encryption model and how to report a
  vulnerability
