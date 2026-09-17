Real captured payloads, verified 2026-09-17 against a live doorbell (see plan:
Verification #1): model D340W, firmware v3.0.0.6460_2605271708, protocol v20.

- `device_info.json`: real `info` output. `serial` is redacted (replaced with
  `"REDACTED"`); everything else is the real response.
- `storage.json`: real `storage status` output, unmodified. Confirms the
  fields are nested under `data.items[0]`, not flat under `data`, which the
  original plan assumed.
- `vod_search.json`: **not** the real capture verbatim. The real `vod search`
  response had 1324 items covering ~2 weeks of real front-door detection
  history; committing that would put a record of when people/vehicles were
  seen at the door into git history indefinitely. Instead this holds one
  representative item per distinct `recordType` value actually observed (14
  combinations, e.g. `"md"`, `"md,people"`, `"people,vehicle"`,
  `"md,people,vehicle,visitor"`), with real field values (`fileSize`,
  `streamType`, `recordType`) but dates shifted onto a fixed placeholder day
  (`2026-04-17`), preserving real times-of-day and exact clip durations
  without the real calendar dates. `name` is regenerated to match the shifted
  `startTime`, consistent with the real device's `01<YYYYMMDDHHMMSS>` naming.

Confirmed against these fixtures, superseding the plan's original guesses:

- `name` (not `fileName`), `startTime`, `endTime`, `fileSize` (not `size`),
  `recordType` (not `type`), `streamType` (not `stream`); the array is under
  `data.items` (not `data.files`).
- `recordType` can be a **comma-separated list** of simultaneously-true types
  (e.g. `"md,people,vehicle"`), not a single enum value from the documented
  vocabulary. Never assume it's exactly one of the documented values.
- `name` values have no file extension (e.g. `"0120260916111127"`), unlike
  the SKILL.md examples (`"20260417_120000.mp4"`).
- `storage status`'s fields are nested under `data.items[0]`.

`ReolinkCliProvider`'s parsing (`_NAME_KEYS`, `_TYPE_KEYS`, etc. in
`reolink_cli.py`) tries these confirmed keys first and falls back to the
originally-documented spellings, kept only in case a different model or
protocol version genuinely uses them; none of the fallbacks have been
observed on real hardware.
