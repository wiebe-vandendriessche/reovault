# Dashboard

The container serves the dashboard on port 8080: a single-page app on a typed
JSON API, updated live over Server-Sent Events, and installable as a PWA (Add
to Home Screen) on a phone. Log in with the `[web] email` and the password set
by `reovault web set-password`.

**Overview** and **Devices** cover every camera. The other pages (Health,
Footage, Runs, Problems) show the camera picked in the header; the choice is
kept in the URL as `?device=`, so a link opens on the same camera.

## Overview

The home page. The archive's disk (ReoVault's share, free space, how long the
room left lasts at the current rate, and the retention policy), then one tile
per camera's SD card: how full it is, a timeline of what's on it coloured by
archived, waiting or failed, and a verdict:

* **All archived**: nothing on the card is waiting.
* **Waiting**: clips on the card that aren't archived yet. Normal between runs.
* **At risk**: the card is looping and the oldest waiting clip will be
  overwritten soon. This is the same condition the `coverage` alert fires on.
* **Not mounted**: the camera reports no SD card.
* **No data yet**: no archive run has sampled the card yet.

Clips the card overwrote before they were archived are reported as a warning
on top of the verdict (see [Problems](#problems)).
[Architecture](architecture.md#scheduler) has the exact thresholds. The
Overview is built only from stored samples, so loading it never contacts a
camera. A card's sample is refreshed after every archive run.

## Health

One camera in detail: its SD card, recent runs, today's footage, footage
archived and detections per type over the last 30 days, and projected growth.

The manual actions live here:

* **Run now** archives the last 48 hours, the same window as a scheduled run.
* **Backfill** archives a time range you pick, in the camera's own timezone,
  up to 90 days at once. Clips already archived are skipped.
* **Check vault** runs a reconcile pass (see `reovault reconcile` in the
  [CLI reference](cli-reference.md)).
* **Stop** ends a run in progress after the clip it is working on. Clips not
  reached yet are picked up by the next run.

A run in progress shows live progress.

## Footage

Date-first browsing: a month calendar shows activity per day and drills into a
day, with clips grouped by hour and filterable by detection type. The left and
right arrow keys step through days. On a phone, clips open in a bottom-sheet
player that steps through the hour.

Clips play straight from the encrypted vault over HTTP `Range` requests, so
only what is being watched or seeked to is decrypted. **Export** downloads a
date range as one zip, up to 31 days and 5000 clips, one export at a time.

## Runs

Every archive run of the camera, newest first, with what it found, archived
and failed, and the next scheduled runs under **Coming up**. **Only runs with
failures** hides the clean ones. Open a run for its per-recording detail.

## Problems

Recordings that failed or were quarantined. **Retry** queues one recording for
the next run, **Retry all** does the same for every problem recording of the
camera.

Above the list, a **lost** summary counts clips the SD card overwrote before
they could be archived. Nothing can bring those back, so they are never
retried and never alerted on. **Dismiss** acknowledges them so they stop
counting as an open issue. If the camera ever lists a lost clip again, it is
archived normally.

## Devices

Every camera ReoVault knows about, with its status, archive totals, last run
and SD card usage. From here you can:

* **Add a camera** from LAN discovery. The camera password goes straight into
  `reolink-cli`'s own encrypted registry and is never stored by ReoVault (see
  [Security](security.md#credentials)). The camera is appended to
  `[[devices]]` in `reovault.toml`.
* **Turn archiving off or on** (`enabled`). A camera that is off keeps its
  footage.
* **Edit** its name, timezone and, optionally, its own schedule
  (`[devices.schedule]`). Its `alias` and `channel` can't be changed here,
  since they match the camera to its archive.

## Settings

The app-wide part of `reovault.toml`. Every save is written back into the file
(see [Configuration](configuration.md#the-file-is-the-source-of-truth)).

* **Schedule**: the default `[schedule]` every camera follows unless it has
  its own.
* **Retention**: `[retention]`. A policy that would delete footage right away
  asks for confirmation first, showing how much would go.
* **Alerts**: the channels, the alerts open right now, which conditions alert
  (`[alerts.rules]`), and a **Send test** button.
* **Web**: `[web]`. Only the session length, login attempt limits and page
  size can be changed here; the rest is shown read-only.
* **System**: `[storage]` and `[reolink_cli]`, read-only.

A field set by a `REOVAULT_*` environment variable shows a lock and can't be
changed from the dashboard.
