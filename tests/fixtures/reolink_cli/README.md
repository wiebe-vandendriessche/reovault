Empty on purpose. Per CLAUDE.md ("never assume protocol behavior... from
memory") and the plan's Unverified section, `vod search`'s real JSON field
names have not been captured from an actual doorbell yet.

When Phase 1 runs on the homelab (see plan: Verification #1), commit the real
captured payloads here:

- `device_info.json`
- `storage.json`
- `vod_search.json`

`ReolinkCliProvider`'s parsing tests should then be tightened to pin the exact
field names these fixtures show, replacing the tolerant multi-key guessing in
`reolink_cli.py` (`_NAME_KEYS`, `_START_KEYS`, etc.) with the confirmed keys.
