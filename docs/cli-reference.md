# CLI reference

Run `reovault --help` for the authoritative list. In Docker, prefix commands
with `docker compose exec reovault` (or `docker compose run --rm reovault`
when the container isn't running).

| Command | What it does |
|---|---|
| `reovault daemon` | Back up every enabled camera on its schedule and serve the dashboard. The image's entrypoint. |
| `reovault doctor` | Check the tools, the folders and every camera's repository. Changes nothing. |
| `reovault run --camera ALIAS` | Run one backup of one camera now, in the foreground, and print the result. Shares the daemon's per-camera lock, so the two never overlap. |
| `reovault web set-password` | Set the dashboard password: hidden prompt, written to a 0600 file, never to argv or `reovault.toml`. Needs `[web] email`. |
| `reovault version` | Print the version. |

To restore footage with plain restic instead, see
[Restore without ReoVault](restore-without-reovault.md).
