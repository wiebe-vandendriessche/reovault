# Dashboard

The container serves the dashboard on port 8080: a single-page app on a typed
JSON API, updated live over Server-Sent Events, and installable as a PWA (Add
to Home Screen) on a phone. Log in with the `[web] email` and the password set
chosen on the first-run screen (the setup code is in the container log). Change either under Settings, Web.

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

Each tile shows the card's fill, how many days it holds, what's backed up, and
the last and next backup, with **Back up now** and **Stop**. A timeline shows the
card from its oldest clip to now, coloured backed up, waiting, failing or
lost. Last come the destinations: role, size against its limit, and whether a
copy is still seeding.

## Footage

Pick a day on the month calendar (the bar under each day shows how busy it
was, a red dot means clips were lost) and its clips appear grouped by hour,
filterable by detection type. The left and right arrow keys step through
days. **Download** gets one clip out of the backup. The **Download** button in the
header gets a whole range: an hour, a day, the last 3 or 7 days or your own
span, from one or several cameras, everything or only person and vehicle
clips, as one zip with a folder per camera. Each hour also has **Download this
hour**. All of it streams straight out of restic, from the fastest destination
that holds the clip.

There is no player: clips still on the SD card play in the Reolink app, and
this page is for what the card no longer has.

## Activity

Every backup run with what it backed up, and two short lists when there's
something to explain: clips that keep failing to download (still on the card,
retried once a day), and clips lost this week (overwritten before they could
be backed up).

## Cameras

Every camera with its status and totals. **Add camera** finds cameras on your
network; ReoVault logs in once to check, then saves the camera, password
included, in `reovault.toml` (it is never shown again). A demo camera needs no
hardware. The switch turns backups off or on (backups already made stay).
**Edit** changes the name, timezone, address and login; a new address or login
is checked with the camera before it's saved.

## Settings

* **Destinations**: connect, limit and disconnect destinations, and pick the
  primary. Anything that could delete backups (a tighter keep-days or size
  limit, turning one off, a new primary, disconnecting) asks for your password
  again and says how much would go.
* **Schedule**: how often cameras are backed up.
* **Recovery**: where the restic password lives, the recovery sheet, and the
  state of the config backup in each destination.
* **Alerts**: the channels, what's open right now, the rules, and **Send test**.
* **Web**: change the password or email, session length, login limits and the
  update check; the rest is read-only.
* **System**: storage paths and tool versions, read-only.

Every save is written into `reovault.toml`. A field set by an environment
variable shows a lock.
