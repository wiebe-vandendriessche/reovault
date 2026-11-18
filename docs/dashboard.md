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

Next to each row a few words say what needs attention, each in its own
colour ("3 lost, 6615 waiting"), or "all backed up"; screen readers get the
same text. Hover a stretch for its times and how many clips it holds. The footer has the
last and next backup, with **Back up now** and **Stop**. Last come the
destinations: role, size against its limit, and whether a copy is still
seeding.

### Problems and how they clear

Every problem on the overview either clears by itself once its cause is gone,
or comes with the button or link that resolves it. None stays forever.

| Problem | Clears | From the dashboard |
|---|---|---|
| The destination can't be reached | as soon as it answers again (shown once, instead of every camera failing) | **Open destinations** |
| Backups are failing (two in a row) | on the next good backup | **Back up now** |
| No backup since ... (6 hours, or 3 intervals) | on the next good backup | **Back up now** |
| Backups are waiting (size limit) | once the copies catch up | **Open destinations** |
| Camera unreachable | when it answers | |
| Clips at risk | when they are backed up | **Back up now** |
| Clips keep failing | when they back up, or the card overwrites them | **Retry clips**, **Give up on them** (also per clip on Activity) |
| Check found damage (twice in a row) | after a clean check | **Check again** |
| Upkeep is failing (retention or check) | on the next good pass | **Check again** |
| Size limit cut backups short | a week after the last such cut, or at once when you change the limit or keep-days | **Open destinations** |
| Size limit too small, over its size limit, copy is behind, can't be reached | when the cause is gone | **Open destinations** |
| Alerts have nowhere to go | when a channel is set up | **Open alert settings** |
| Settings backup failing | on the next good settings backup | **Open recovery** |

When the destination (say Nextcloud) stops answering, nothing needs doing:
every camera keeps reading its card and tries again each interval, clips wait
on the SD cards, and the first run after the destination is back uploads them.
Only "clips at risk" is still alerted per camera, because that is the one that
asks for action if an outage lasts as long as the card holds footage.

A repository lock left behind by a restic that died (a timeout, a restart, a
destination that hung) is removed by ReoVault itself; it never needs a hand.
**Give up** marks clips as lost: they are never downloaded again.

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
