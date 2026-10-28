# Architecture

ReoVault does one thing: it copies the clips on each camera's SD card into a
[restic](https://restic.net/) backup, before the card's loop recording
overwrites them. Everything that makes a backup trustworthy (encryption,
deduplication, integrity checks, retention) is restic's job, not ReoVault's.

```
camera SD card --reolink-cli vod download--> local batch --restic backup--> destination
                                             (one batch at a time)          (local disk, Nextcloud,
                                                                             OneDrive, S3, ... via rclone)
```

## Modules

| Module | Job |
|---|---|
| `camera.py` | Talks to cameras through the official `reolink-cli`: search a card, batch-download clips, read the card's status, discover and add cameras. Also `DemoCamera`, a pretend camera for trying ReoVault without hardware. |
| `restic.py` | A thin wrapper around the pinned `restic` binary: open/init a repository, back up, list snapshots, `forget`, `check`, `dump`. |
| `backup.py` | One camera's backup run, below. The core. |
| `worker.py` | One thread per enabled camera: runs on its interval, plus that camera's daily retention and weekly check. |
| `status.py` | One plain verdict per camera: up to date, catching up, at risk, failing, no SD card. |
| `alerts.py` | ntfy, webhook and email, driven by what's wrong right now. |
| `db.py` | SQLite: the clip index and the run history. |
| `config.py`, `config_store.py` | `reovault.toml`, written back by the dashboard with comments kept. |
| `web/` | The `/api/v1` JSON API and the built dashboard. |

## A backup run

`backup.py` runs one camera, in that camera's thread:

1. **Recover.** Half-finished downloads from a crash are deleted. Clips that
   were downloaded and verified but not yet uploaded are uploaded first.
2. **Open the repository.** Each camera has its own restic repository at
   `<destination>/<alias>`. A missing repository is created only if this
   camera never backed anything up there before. If it did, a missing
   repository means a disk isn't mounted or the path is wrong, and the run
   stops with that message instead of starting a fresh, empty backup.
3. **List the card.** On the first run, once a day and after an error, the
   whole card is read, one camera-local day at a time, going back until three
   empty days in a row. Otherwise only the last couple of hours are searched.
4. **Batches**, until nothing is left:
   * **Pick** up to 2 GB of clips: the newest first (recorded since the last
     run), then the oldest, because the oldest is what the card overwrites
     next. A first run on a full card works through the backlog oldest-first
     while new clips still go out within the hour.
   * **Download** them with one `reolink-cli vod download` call, which shares
     one camera connection.
   * **Verify** each file's size against what the card reported. A short file
     gets one retry after a pause (a D340W quirk); a clip that keeps failing
     moves to the back and is retried once a day.
   * **Stage** the verified files in `<staging>/<alias>/batch/<date>/`.
   * **Upload** with `restic backup`. Only when restic exits 0 with a snapshot
     id are the clips marked backed up, and only then are the local files
     deleted.
   * If the upload fails, the batch stays on disk and the run stops: nothing
     more is downloaded until the destination is back, so local disk never
     holds more than one batch. The clips wait on the SD card.

**The invariant:** a clip counts as backed up only after its size matched,
restic confirmed a snapshot, and the database recorded it. A crash anywhere
before that just means the next run uploads the same files again; restic
stores the data once.

**Lost clips:** when a download fails, ReoVault searches that day on the card
again. A clip the card no longer lists was overwritten before it could be
backed up: it's marked lost, counted, and never retried. Any other failure is
retried. The decision never depends on error wording, which differs between
models and reolink-cli versions.

## Snapshots and retention

Every batch becomes one restic snapshot of the same folder,
`<staging>/<alias>/batch`, with the host name fixed to `reovault`. Because the
path and host never change, all of a camera's snapshots form one group, and
`restic forget --keep-within <N>d` keeps "the last N days" of them.

`--keep-within` counts back from the newest snapshot, not from today. If
backups stop for two weeks, nothing old is deleted in the meantime. After
`forget`, clips whose snapshot is gone are marked expired in the index, so
they are never downloaded again even if they're still on the card.

Weekly, each repository is checked with `restic check --read-data-subset 500M`,
which reads a slice of the stored data back to catch bit rot early.

## The index

SQLite remembers every clip a card ever listed and what became of it:
waiting, backed up (with its repository, snapshot and path), lost or
expired. It exists so that:

* the dashboard never has to ask a remote destination anything to show a
  calendar or an overview;
* a clip is never downloaded twice.

restic stays the source of truth. A download uses the snapshot and path
stored in the index, so it keeps working after the destination changes.

## Downloads

A clip is downloaded with `restic dump`, streamed straight to the browser. A
day downloads as one zip, built while streaming. Neither uses temporary
files. `dump` runs without a repository lock: the weekly `check` holds an
exclusive one for as long as it reads, and a download must not wait hours for
it.

## Concurrency

Each camera has one thread, and everything that touches that camera's
repository (backups, `forget`, `check`) happens in it, one step after
another. Cameras run in parallel; a camera never does. `reovault run` shares
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
