# Dashboard

The container serves the dashboard on port 8080: a single-page app on a typed
JSON API, updated live over Server-Sent Events, and installable as a PWA (Add
to Home Screen) on a phone. Log in with the `[web] email` and the password set
by `reovault web set-password`.

## Overview

The home page answers one question: is every SD card backed up? First
"Nothing needs attention", or a list of what does. Then one tile per camera:

* **Up to date**: every clip on the card is backed up.
* **Catching up**: clips are waiting, with how much and about how long.
* **At risk**: the oldest waiting clip is close to being overwritten by the
  card's loop recording. This is also an alert.
* **Failing**: the last backup failed, with the reason.
* **No SD card**: the camera reports none.

Each tile shows the card's fill, how many days it holds, what's backed up, and
the last and next backup, with **Back up now** and **Stop**. Last comes the
destination and how long backups are kept.

## Footage

Pick a day on the month calendar (the bar under each day shows how busy it
was, a red dot means clips were lost) and its clips appear grouped by hour,
filterable by detection type. The left and right arrow keys step through
days. **Download** gets one clip out of the backup; **Download day** gets the
whole day as a zip. Both stream straight out of restic.

There is no player: clips still on the SD card play in the Reolink app, and
this page is for what the card no longer has.

## Activity

Every backup run with what it backed up, and two short lists when there's
something to explain: clips that keep failing to download (still on the card,
retried once a day), and clips lost this week (overwritten before they could
be backed up).

## Cameras

Every camera with its status and totals. **Add camera** finds cameras on your
network; the password goes straight into reolink-cli's own encrypted registry
and is never stored by ReoVault. A demo camera needs no hardware. The switch
turns backups off or on (backups already made stay). **Edit** changes the
name and timezone.

## Settings

* **Backups**: the destination with a **Test** button that checks every
  camera's repository, the interval, and how long to keep backups. Shortening
  retention asks first and says how much would go.
* **Alerts**: the channels, what's open right now, the rules, and **Send test**.
* **Web**: session length and login limits; the rest is read-only.
* **System**: storage paths and tool versions, read-only.

Every save is written into `reovault.toml`. A field set by an environment
variable shows a lock.
