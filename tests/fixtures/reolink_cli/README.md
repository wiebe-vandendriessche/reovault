Captured payloads from a real Reolink camera (protocol v20), used to
fixture-pin `ReolinkCliProvider`/`ReolinkCliRegistry` parsing so a
`reolink-cli` schema change fails loudly here instead of drifting silently.

- `device_info.json`: `info` output; `serial` is redacted.
- `discover.json`: `discover --output json` output, no gateway needed. `uid`
  and `mac` are redacted; `host`/`protocol`/`scopes` are real. Model comes
  from an `onvif://...hardware/<MODEL>` scope entry, the friendly name from a
  `reolink-lan://name/<NAME>` entry. There's no dedicated `model`/`name`
  field on the discovery record itself.
- `device_list.json`: `device list --output json` output, no gateway needed.
  `hasPassword` is a boolean, the password itself never appears; `channel`
  can be `null`, not just an int.
- `storage.json`: `storage status` output. Fields are nested under
  `data.items[0]`, not flat under `data`.
- `vod_search.json`: **not** a verbatim capture. The real response covered
  weeks of detection history at one address; committing that would put a
  timestamped activity record into git history indefinitely. This holds one
  representative item per distinct `recordType` combination observed (14
  total), with real `fileSize`/`streamType`/`recordType` values but
  synthetic times and a single placeholder day, durations preserved.

Confirmed field shapes (parser tries these first, falls back to
originally-assumed spellings that have never actually been observed):

- `name` (not `fileName`), `startTime`, `endTime`, `fileSize` (not `size`),
  `recordType` (not `type`), `streamType` (not `stream`); the array is under
  `data.items` (not `data.files`).
- `recordType` can be a **comma-separated list** of simultaneously-true
  types (e.g. `"md,people,vehicle"`), never assume a single enum value.
- `name` values have no file extension.
