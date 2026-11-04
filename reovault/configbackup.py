"""ReoVault's own config, backed up next to the footage.

Footage is only half of a recovery: without `reovault.toml` (the cameras, with
their passwords) and `rclone.conf` a rebuilt server doesn't know where to look
or how to log in. Whenever those files change, they are backed up into each destination's
`reovault-config` repository (restic-encrypted like everything else, same
password), keeping the last 30 versions. To recover: restore that repository
into /config (see docs/restore-without-reovault.md), start ReoVault, and the
index rebuilds itself from the footage.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from reovault.config import DestinationConfig, Settings
from reovault.db import Db, now_iso
from reovault.destinations import restic_for, update_status
from reovault.logging import get_logger
from reovault.tools import ToolError

logger = get_logger(__name__)

REPO = "reovault-config"  # sits next to the camera repositories, so no camera id may be this
KEEP_LAST = 30
EVERY_SECS = 3600


def digest(files: dict[str, Path]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(files[name].read_bytes())
    return h.hexdigest()


class ConfigBackup:
    def __init__(
        self,
        settings: Callable[[], Settings],
        db: Db,
        files: Callable[[], dict[str, Path]],
    ) -> None:
        self.settings, self.db, self.files = settings, db, files
        self._quit = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="config-backup", daemon=True).start()

    def shutdown(self) -> None:
        self._quit.set()

    def _loop(self) -> None:
        delay = 60  # soon after start, then hourly
        while not self._quit.wait(delay):
            delay = EVERY_SECS
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - never ends the thread
                logger.exception("configbackup.crashed")

    def run_once(self) -> None:
        settings = self.settings()
        files = {n: p for n, p in self.files().items() if p.is_file()}
        if not files:
            return
        want = digest(files)
        for dest in (d for d in settings.destinations if d.enabled):
            if self.db.get_setting(f"config_backup_hash:{dest.name}") == want:
                continue
            try:
                self._one(settings, dest, files)
            except ToolError as exc:
                logger.error("configbackup.failed", destination=dest.name, error=str(exc))
                update_status(self.db, dest.name, config_error=str(exc))
                continue
            self.db.set_setting(f"config_backup_hash:{dest.name}", want)
            update_status(self.db, dest.name, config_ok=now_iso(), config_error=None)

    def _one(self, settings: Settings, dest: DestinationConfig, files: dict[str, Path]) -> None:
        restic = restic_for(settings, REPO, dest)
        state = restic.state()
        if state == "wrong_password":
            raise ToolError("the restic password doesn't open the config repository")
        if state == "missing":
            if self.db.repo_seen(dest.name, REPO):
                raise ToolError(
                    "the destination has no config repository any more, but there was one "
                    "before. Check that it is reachable: nothing will be created."
                )
            restic.init()
        self.db.mark_repo_seen(dest.name, REPO)
        # A fixed folder, so every snapshot is one group; removed right after.
        stage = settings.storage.data_dir / "config-backup"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, mode=0o700)
        try:
            for name, path in files.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            restic.backup(stage, timeout=600)
            restic.forget_last(KEEP_LAST)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
