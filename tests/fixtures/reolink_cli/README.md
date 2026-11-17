Captured payloads from a real Reolink camera (protocol v20), used to
fixture-pin the parsing in `reovault/camera.py` so a
`reolink-cli` schema change fails loudly here instead of drifting silently.

- `discover.json`: `discover --output json` output, no gateway needed. `uid`
  and `mac` are redacted; `host`/`protocol`/`scopes` are real. Model comes
  from an `onvif://...hardware/<MODEL>` scope entry, the friendly name from a
  `reolink-lan://name/<NAME>` entry. There's no dedicated `model`/`name`
  field on the discovery record itself.
- `device_info.json`: `info --output json` output (D340W), serial redacted.
  The firmware and hardware fields are `firmware` and `hardware_version`.
- `storage.json`: `storage status` output. Fields are nested under
  `data.items[0]`, not flat under `data`.
- `vod_search.json`: **not** a verbatim capture. The real response covered
  weeks of detection history at one address; committing that would put a
  timestamped activity record into git history indefinitely. This holds one
  representative item per distinct `recordType` combination observed (14
  total), with real `fileSize`/`streamType`/`recordType` values but
  synthetic times and a single placeholder day, durations preserved.
- `vod_search_dst_fold.json`: a reduced `vod search` of the driveway camera
  (Europe/Brussels) across the night the clocks went back, 2026-10-25,
  captured 2026-11-17 with reolink-cli 0.20.0. The card lists both passes
  through 02:00-03:00 by local time only, sorted as one hour, with no field
  that tells them apart. Kept verbatim: the two clips that share the name
  `0120261025024413` (same `startTime`, different `endTime` and `fileSize`)
  and the clip that ends after the change. The other items are a few
  neighbours; the last `fileSize` is a placeholder.

Confirmed field shapes (parser tries these first, falls back to
originally-assumed spellings that have never actually been observed):

- `name` (not `fileName`), `startTime`, `endTime`, `fileSize` (not `size`),
  `recordType` (not `type`), `streamType` (not `stream`); the array is under
  `data.items` (not `data.files`).
- `recordType` can be a **comma-separated list** of simultaneously-true
  types (e.g. `"md,people,vehicle"`), never assume a single enum value.
- `name` values have no file extension.
