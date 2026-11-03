# Architecture

ReoVault does one thing: it copies the clips on each camera's SD card into a
[restic](https://restic.net/) backup, before the card's loop recording
overwrites them. Everything that makes a backup trustworthy (encryption,
deduplication, integrity checks, retention) is restic's job, not ReoVault's.

```
camera SD card --reolink-cli vod download--> local batch --restic backup--> primary destination
                                             (at most two batches)          (a local disk, ideally)
                                                                                  |
                                                            restic copy (own threads, never block a backup)
                                                                                  v
                                                              copy destinations (Nextcloud, OneDrive, S3, ... via rclone)
```

## Modules

| Module | Job |
|---|---|
| `camera.py` | Talks to cameras through the official `reolink-cli`: search a card, batch-download clips, read the card's status, discover and add cameras. Also `DemoCamera`, a pretend camera for trying ReoVault without hardware. |
| `restic.py` | A thin wrapper around the pinned `restic` binary: open/init a repository, back up, list snapshots, `forget`, `check`, `dump`. |
| `backup.py` | One camera's backup run, below. The core. |
| `worker.py` | One thread per enabled camera: runs on its interval, plus that camera's daily retention and weekly check. |
| `destinations.py`, `retention.py` | The 3-2-1 side: one thread per copy destination (`restic copy`, its own retention and size limit, weekly check), and the pure functions that choose which snapshots to forget. |
| `rclone.py` | Connects rclone remotes from the dashboard through `rclone config create`; ReoVault never parses `rclone.conf`. |
| `configbackup.py` | Backs up ReoVault's own config (`reovault.toml`, `rclone.conf`, the camera registry) into each destination. |
| `updates.py` | The optional daily "is there a newer release" check. |
| `status.py` | One plain verdict per camera: up to date, catching up, at risk, failing, offline, no SD card, and the SD-card timeline. |
| `alerts.py` | ntfy, webhook and email, driven by what's wrong right now. |
| `db.py` | SQLite: the clip index and the run history. |
| `config.py`, `config_store.py` | `reovault.toml`, written back by the dashboard with comments kept. |
| `web/` | The `/api/v1` JSON API and the built dashboard. |

## A backup run

`backup.py` runs one camera, in that camera's thread:

1. **Open the repository.** Each camera has its own restic repository at
   `<destination>/<alias>`. A missing repository is created only if this
   camera never backed anything up there before. If it did, a missing
   repository means a disk isn't mounted or the path is wrong, and the run
   stops with that message instead of starting a fresh, empty backup.
2. **Catch the index up** if the repository has snapshots the database
   doesn't know (see [The index](#the-index)).
3. **Recover.** Half-finished downloads from a crash are deleted. Clips that
   were downloaded and verified but not yet uploaded are uploaded first.
4. **List the card.** On the first run, once a day and after an error, the
   whole card is read, one camera-local day at a time, going back until three
   empty days in a row. Otherwise only the last couple of hours are searched.
5. **Batches**, until nothing is left:
   * **Pick** up to 2 GB of clips: the newest first (recorded since the last
     run), then the oldest, because the oldest is what the card overwrites
     next. A first run on a full card works through the backlog oldest-first
     while new clips still go out within the hour.
   * **Download** them into `<staging>/<alias>/incoming/` with one
     `reolink-cli vod download` call, which shares one camera connection.
     This happens while the previous batch uploads (see below). If the whole
     call fails while the camera still answers, the batch is downloaded one
     clip at a time, so a single bad clip can't hold up the rest.
   * **Verify** each file's size against what the card reported. A short file
     gets one retry after a pause (a D340W quirk); a clip that keeps failing
     moves to the back and is retried once a day.
   * **Wait** for the previous batch's upload, then **stage** the verified
     files in `<staging>/<alias>/batch/<date>/`.
   * **Upload** with `restic backup` on a background thread, while the next
     batch downloads. Only when restic exits 0 with a snapshot id are the
     clips marked backed up, and only then are the local files deleted.
   * If an upload fails, the next batch is still staged next to it and the
     run stops: nothing more is downloaded until the destination is back.
     The next run uploads both in one snapshot. Local disk holds at most two
     batches (one uploading, one downloading), about 4.8 GB per camera. The
     clips wait on the SD card.

Overlapping the two cuts a first backup of a full card by about a third:
download (about 4 to 6 MB/s) and upload no longer take turns.

**The invariant:** a clip counts as backed up only after its size matched,
restic confirmed a snapshot, and the database recorded it. A crash anywhere
before that just means the next run uploads the same files again; restic
stores the data once.

**Lost clips:** when a download fails, ReoVault searches that day and the next
on the card again. A clip counts as lost only with proof: the card no longer
lists it *and* it is older than the oldest clip the card still has (cards
overwrite oldest first). It's then counted and never retried. An empty or
truncated listing proves nothing, so that clip is retried. The decision never
depends on error wording, which differs between models and reolink-cli
versions.

**Clips without a size** (the card may list a clip that is still recording)
wait until the card reports one: without it a download can't be verified.

## Snapshots and retention

Every batch becomes one restic snapshot of the same folder,
`<staging>/<alias>/batch`, with the host name fixed to `reovault`. Because the
path and host never change, all of a camera's snapshots form one group.

Retention counts back from the camera's newest snapshot, not from today. If
backups stop for two weeks, nothing old is deleted in the meantime. After
a snapshot is forgotten, clips whose snapshot is gone are marked expired in the index, so
they are never downloaded again even if they're still on the card.

Weekly, each repository is checked with `restic check --read-data-subset 500M`,
which reads a slice of the stored data back to catch bit rot early.

## Destinations, copies and quotas

Cameras back up into the **primary** only. Every other destination is a
**copy**: after each commit its thread wakes and copies the new snapshots with
`restic copy`, one snapshot per call, so progress is recorded and retention can
run in between. A copy repository is created with `init --copy-chunker-params`
(so deduplication works across both), and never where the camera already has
history, for the same dropped-mount reason as the primary. Seeding a new
destination with a whole history runs in its own thread for as long as it takes.

`locations(camera, source_snapshot, destination, snapshot_id)` records where
each snapshot lives. restic gives a copied snapshot a new id but keeps the
original in its `original` field, which is how the copy is matched back to the
clip's snapshot. A clip is **expired** only when no destination holds its
snapshot, and a download reads from the primary, then any local destination,
then the rest.

**Retention is chosen by ReoVault**, which runs `forget <ids> --prune` (not
`--keep-within`), because a destination's `keep_days` and `max_gb` differ:

* the primary may forget a snapshot only when every copy that wants it has it;
* a camera's newest snapshot is never forgotten (it carries the clip list);
* a copy only takes snapshots inside its own `keep_days`, and above a floor that
  rises whenever its quota forgets there, so copying and retention never loop;
* a size limit estimates use as the sum of the clip sizes in the snapshots,
  trims the oldest at 90% down to 85% (headroom for manifests and prune), and
  alerts when it had to cut younger than `keep_days`, when even the newest
  backups don't fit, or when copies it is waiting for block it.

## The index

SQLite remembers every clip a card ever listed and what became of it:
waiting, backed up (with its repository, snapshot and path), lost or
expired. It exists so that:

* the dashboard never has to ask a remote destination anything to show a
  calendar or an overview;
* a clip is never downloaded twice.

restic stays the source of truth. A download uses the snapshot and path
stored in the index, so it keeps working after the destination changes.

**Every snapshot carries the index.** Before each upload, ReoVault writes
`batch/.reovault/index.json` into the batch: the camera's whole index (every
backed-up clip with its snapshot, every lost and expired clip, detection types
included). If the database is lost, restored from an older copy, or a crash
left a snapshot unrecorded, the next run notices restic has snapshots the
database doesn't know, reads the newest snapshot's index (two restic calls),
and carries on. Nothing is downloaded again and old backups stay visible. Its
own clips are only trusted if they really are in that snapshot, because a
`restic backup` that exits 3 (a file unreadable) still writes one.

## Downloads

A clip is downloaded with `restic dump`, streamed straight to the browser. A
range (an hour to several days, one or more cameras, all clips or only some
detection types) downloads as one zip with a folder per camera, built while
streaming. Neither uses temporary files. `dump` runs without a repository lock: the weekly `check` holds an
exclusive one for as long as it reads, and a download must not wait hours for
it.

## Concurrency

Each camera has one thread, and everything that touches that camera's
*primary* repository (backups, `forget`, `check`) happens in it, one step after
another; the only helper is the upload thread of the run in progress, joined
before the run ends. Each copy destination has its own thread and only writes
to its own repositories, reading the primary with `restic copy`. Lock-taking
restic commands pass `--retry-lock 10m`, so the two sides wait for each other
instead of failing. Cameras run in parallel; a camera never does. `reovault run` shares
the same per-camera lock file as the daemon.

## Why these tools

* **reolink-cli** is Reolink's own, official tool. It downloads over the
  camera's native protocol: on a D340W about 1.7 MB/s, against about 0.8 MB/s
  for the camera's HTTPS API. See [Compatible devices](compatibility.md).
* **restic** turns "copy files somewhere" into a verified, encrypted,
  deduplicated backup with retention and integrity checks, and anyone can
  restore it without ReoVault: see [Restore without ReoVault](restore-without-reovault.md).
* **rclone** gives restic every cloud and home-server destination
  (Nextcloud, OneDrive, Google Drive, S3, SFTP, ...) from one `rclone.conf`.

All three are pinned and checksum-verified in the image, and ReoVault refuses
to start against a different version.
