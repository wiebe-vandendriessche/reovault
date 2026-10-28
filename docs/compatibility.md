# Compatible devices

ReoVault talks to cameras through [`reolink-cli`](https://github.com/reolink/reolink-cli),
not a reimplementation of Reolink's protocol, so in principle any device
`reolink-cli` can search and download recordings from is a candidate. The
table below tracks what's actually been run against real hardware.

| Model | Discovery / connection | Backup (search, download, verify) |
|---|---|---|
| Reolink Video Doorbell D340W | Verified | Verified. Test fixtures come from this device. |
| Reolink E1 Outdoor Pro | Verified | Verified: 2 GB batches backed up to a local repository and to Nextcloud. |

## Measured (2026-10-28)

Nothing else was using the cameras during these runs.

| | Download, one batch | Two batches in parallel |
|---|---|---|
| Video Doorbell D340W (Wi-Fi, reolink-cli 0.19.0) | 4.1 MB/s | 3.9 MB/s, no gain |
| E1 Outdoor Pro (Ethernet, reolink-cli 0.19.0) | 6.2 MB/s | |

* **Both cameras at once** run at nearly full speed each (4.5 and 5.8 MB/s)
  through one reolink-gateway.
* **A full ReoVault batch** (2 GB, reolink-cli 0.20.0 in the image) to a
  Nextcloud over WebDAV: download 403 to 510 s, upload 350 s (about 6.1 MB/s).
  A clip restored from that Nextcloud matched a fresh download from the
  camera byte for byte.
* **A busy day:** about 9.4 GB of motion clips in 6 hours (19 MB clips at about
  4.6 Mbit/s), so 25 to 35 GB per day, against roughly 350 GB per day the
  camera can be read at.
* **Search:** 6 hours of clips in 3.4 s; ReoVault searches one camera-local
  day at a time.
* **Batch downloads** (`vod download a b c --directory D`) exit 0 even when a
  name can't be fetched, and report it under `data.skipped`. Downloaded sizes
  match `vod search`'s `fileSize` exactly.
* **reolink-cli's gateway** is required for search, storage status and
  download.
* The camera's HTTP API was not enabled; the published figure for its HTTPS
  download path is about 0.8 MB/s (see
  [reolink-downloader](https://github.com/deviantintegral/reolink-downloader)).

A camera that changes IP address (DHCP) can no longer be reached under its
old registry entry. Give it a DHCP reservation, or update the entry with
`reolink-cli device update <alias> --host <ip>`.

If you run ReoVault against a device that isn't listed here, please open an
issue with the model and what worked. That's how this table grows.
