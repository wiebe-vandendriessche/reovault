# Restore without ReoVault

Your footage is in ordinary restic repositories, one per camera. Anyone with
[restic](https://restic.net/) and the repository password can get it back,
with or without ReoVault: if the server died, or years from now.

You need:

* `restic` (any recent version) and, for a cloud destination, `rclone` with
  the same `rclone.conf`;
* the restic password (the `restic_password` secret);
* the destination, e.g. `rclone:nextcloud:reovault`, plus the camera's alias.

```bash
export RESTIC_PASSWORD_FILE=./secrets/restic_password.txt
export RCLONE_CONFIG=./config/rclone/rclone.conf   # only for rclone: destinations
REPO=rclone:nextcloud:reovault/front-door          # <destination>/<alias>
```

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

The clips end up under `restored/data/staging/<alias>/batch/<date>/`.

## Browse

On a Linux or macOS machine with FUSE:

```bash
mkdir -p /tmp/footage && restic -r "$REPO" mount /tmp/footage
```

Every snapshot then appears as a folder you can open in a file manager.
