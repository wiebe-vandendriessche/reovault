# Dashboard

The container serves the dashboard on port 8080: a single-page app on a typed
JSON API, updated live over Server-Sent Events, and installable as a PWA (Add
to Home Screen) on a phone. Log in with the email and password you chose on
the first-run screen (its one-time setup code is in the container log).
Change either under Settings, Web.

## Overview

The home page answers one question: is every SD card backed up? First
"Nothing needs attention", or a list of what does. Then one tile per camera:

* **Up to date**: every clip on the card is backed up.
* **Catching up**: clips are waiting, with how much and about how long.
* **At risk**: the oldest waiting clip is close to being overwritten by the
  card's loop recording. This is also an alert.
* **Failing**: the last backup failed, with the reason.
* **Offline**: the camera isn't answering (powered off, or off the network).
  Nothing is wrong with any clip; the tile says since when.
* **No SD card**: the camera reports none.
* **No backup yet**: nothing has finished for this camera so far.

Under the verdict, each tile shows **where that camera's footage is**, one
row per place on the same time axis. The list above the tiles picks how far
back it looks: since the SD card starts (the default), the last week, month,
3 or 6 months, year or 2 years (only as far back as you have backups), or
everything ReoVault has kept:

* **SD card**: what the card holds now, from its oldest clip (a thin line marks
  where the card starts) to now. Green is already backed up, blue still waiting,
  red keeps failing to download, orange was lost (overwritten before it could be
  backed up, so it sits just before the card's start).
* **The primary destination**: green is what it holds, blue what still has to
  come from the card. A gap is footage it no longer keeps (its keep-days or size
  limit), or time when nothing was recorded.
* **Each copy destination**: the same, where blue also means footage the primary
  has that this copy wants but hasn't copied yet.

Hover a stretch for its times and how many clips it holds. The footer has the
last and next backup, with **Back up now** and **Stop**. Last come the
destinations: role, size against its limit, and whether a copy is still
seeding.

## Footage

Pick a day on the month calendar (the bar under each day shows how busy it
was, a red dot means clips were lost) and its clips appear grouped by hour,
filterable by detection type. The left and right arrow keys step through
days. **Download** on a clip gets that clip out of the backup, and each hour
has **Download this hour**. The **Download** button in the header gets a range
as one zip with a folder per camera: this day, the last 3 or 7 days, or your
own from/until, from one or several cameras, every clip or only the detection
types you pick. A preview says how many clips and how big before you start.
All of it streams straight out of restic, from the primary destination when
it still holds the clip, otherwise from a local one, otherwise from a cloud
copy.

There is no player: clips still on the SD card play in the Reolink app, and
this page is for what the card no longer has.

## Activity

Every backup run with what it backed up, and two short lists when there's
something to explain: clips that keep failing to download (still on the card,
retried once a day), and clips lost this week (overwritten before they could
be backed up). Click a run's start time for its detail: each batch (size,
download and upload time, snapshot) and what happened to each clip. A run cut
off by a restart shows as **Stopped**, not failed: the next run carries on.

## Cameras

Every camera as a device: its model and firmware (asked from the camera, at
most every 10 minutes), address and user, and how full its SD card is and how
many days it holds. How its backups are doing is on the Overview. **Add camera** finds cameras on your
network; ReoVault logs in once to check, then saves the camera, password
included, in `reovault.toml` (it is never shown again). A demo camera needs no
hardware. The switch turns backups off or on (backups already made stay).
**Edit** changes the name, timezone, address and login: a new address or user
needs the camera password again, and is checked with the camera before it's
saved. **Remove this camera** takes it (and its password) out of
`reovault.toml`; its backups stay, and adding a camera with the same id brings
them back.

## Settings

* **Destinations**: connect, limit and disconnect destinations, and pick the
  primary. Anything that could delete or orphan backups (a tighter keep-days or
  size limit, turning one off, a new primary, disconnecting) asks for your
  dashboard password again. **Limits** shows how much a new limit would delete;
  **Disconnect** never deletes anything on the destination.
* **Schedule**: how often cameras are backed up.
* **Recovery**: where the restic password lives, the recovery sheet, and the
  state of the config backup in each destination.
* **Alerts**: the channels, what's open right now, the rules, and **Send test**.
* **Web**: change the password or email, session length, login limits and the
  update check; the rest is read-only.
* **System**: storage paths and tool versions, read-only.

Every save is written into `reovault.toml`, except the login password, which
lives in its own file in `data/`. A field set by an environment variable shows
a lock.
