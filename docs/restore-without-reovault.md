# Restore without ReoVault

Your footage is in ordinary restic repositories, one per camera. Anyone with
[restic](https://restic.net/) and the repository password can get it back,
with or without ReoVault: if the server died, or years from now.

You need:

* `restic` (any recent version) and, for a cloud destination, `rclone` with
  the same `rclone.conf`;
* the restic password (the `restic-password` file);
* the destination's `repository` from `reovault.toml` (or the recovery
  sheet), e.g. `rclone:reovault-cloud:reovault`, plus the camera's id.

```bash
export RESTIC_PASSWORD_FILE=./restic-password
export RCLONE_CONFIG=./rclone.conf                 # only for rclone: destinations
REPO=rclone:reovault-cloud:reovault/front-door     # <repository>/<id>
```

Remotes connected from the dashboard are named `reovault-<destination name>`;
one you made by hand keeps its own name.

## Find a clip

Each backup run makes small snapshots of one batch of clips. Clips are stored
by the day they were recorded, under `.../batch/<YYYY-MM-DD>/<name>.mp4`:

```bash
restic -r "$REPO" find '*20261027*'          # every clip recorded on 27 Oct 2026
restic -r "$REPO" find 0120261027221944.mp4  # one clip by its name on the card
```

`find` prints the snapshot id and the full path. Then:

```bash
restic -r "$REPO" dump <snapshot-id> <path> > clip.mp4
```

## Restore everything

Restore every snapshot into one folder (later snapshots never overwrite a
clip, since each clip lives in exactly one):

```bash
mkdir -p restored
restic -r "$REPO" snapshots --json | python3 -c 'import json,sys; [print(s["id"]) for s in json.load(sys.stdin)]' |
  while read -r id; do restic -r "$REPO" restore "$id" --target restored; done
```

The clips end up under `restored/data/staging/<id>/batch/<date>/`.

## The clip list

Every snapshot also holds `.../batch/.reovault/index.json`: ReoVault's list of
every clip of that camera at the time, with its start time (UTC), size,
detection types (`md` motion, `people`, `vehicle`, ...) and the snapshot it
is in. The newest one is complete, so it's a handy table of contents:

```bash
restic -r "$REPO" dump latest "$(restic -r "$REPO" snapshots latest --json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["paths"][0])')/.reovault/index.json" \
  | python3 -m json.tool | less
```

You can ignore these files otherwise; restoring everything (above) just puts
one copy next to the clips.

## Rebuild ReoVault on a new server

The server died but a destination survived? Nothing is lost, as long as you
have the restic password. ReoVault backs up its own config into each
destination whenever it changes (checked hourly, the last 30 versions kept), as the repository `<destination>/reovault-config`: `reovault.toml`
(the cameras with their passwords) and `rclone.conf`. It is encrypted by restic
like everything else: the rclone tokens and camera passwords in it are why the
restic password matters twice.

1. Install ReoVault on the new server and put the restic password back in
   `restic-password` next to `compose.yaml`.
2. Restore the config from any destination:

   ```bash
   restic -r <destination>/reovault-config restore latest --target ./restored-config
   ```

   The files land in `./restored-config/data/config-backup/`. Put
   `reovault.toml` and `rclone.conf` next to `compose.yaml`. For a cloud
   destination you need an `rclone.conf` that reaches it first: the old
   server's recovery sheet says which destinations there were.
3. Start ReoVault. It sees snapshots it has no record of, rebuilds its clip
   index from the newest snapshot of each camera, and carries on without
   downloading anything twice.

## Browse

On a Linux or macOS machine with FUSE:

```bash
mkdir -p /tmp/footage && restic -r "$REPO" mount /tmp/footage
```

Every snapshot then appears as a folder you can open in a file manager.
