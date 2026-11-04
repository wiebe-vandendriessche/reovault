"""The running app: settings, database, workers and alerts, shared by the
daemon and the web API."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reovault import updates as updates_mod
from reovault.alerts import Condition, Notifier, channel_status
from reovault.camera import Gateway
from reovault.config import Settings
from reovault.config_store import ConfigStore
from reovault.configbackup import ConfigBackup
from reovault.db import Db
from reovault.destinations import Destinations, lag, quota_short, status_of
from reovault.status import camera_status
from reovault.web.events import EventBus
from reovault.worker import Workers

# `user:password@` inside a URL, e.g. a rest-server or S3 repository string.
_URL_CREDENTIALS = re.compile(r"(?<=//)[^/@\s]+@")


@dataclass
class Runtime:
    config: ConfigStore
    db: Db
    gateway: Gateway
    bus: EventBus = field(default_factory=EventBus)
    workers: Workers = field(init=False)
    destinations: Destinations = field(init=False)
    config_backup: ConfigBackup = field(init=False)
    notifier: Notifier = field(init=False)

    def __post_init__(self) -> None:
        self.destinations = Destinations(self.settings, self.db, self.bus.publish)
        self.config_backup = ConfigBackup(self.settings, self.db, self.config_files)
        self.workers = Workers(
            self.settings,
            self.db,
            self.gateway,
            self.bus.publish,
            on_backed_up=self.destinations.wake,
        )
        self.notifier = Notifier(
            settings=self.settings, db=self.db, conditions=self.conditions, publish=self.bus.publish
        )

    def settings(self) -> Settings:
        return self.config.current

    def config_files(self) -> dict[str, Path]:
        """What a rebuilt server needs besides the footage (see configbackup.py):
        the config (cameras and their passwords included) and the rclone remotes."""
        files: dict[str, Path] = {}
        if self.config.path is not None:
            files["reovault.toml"] = self.config.path
        rclone_conf = os.environ.get("RCLONE_CONFIG")
        if rclone_conf:
            files["rclone.conf"] = Path(rclone_conf)
        return files

    def scrub_credentials(self, text: str | None) -> str | None:
        return _URL_CREDENTIALS.sub("***@", text) if text else text

    def redact(self, text: str | None) -> str | None:
        """Every error string leaving the process (API answers, alerts) goes
        through this: tool output can carry local paths and repository
        credentials."""
        if not text:
            return text
        s = self.settings()
        for placeholder, path in (
            ("<staging>", s.storage.staging_dir),
            ("<data>", s.storage.data_dir),
        ):
            text = text.replace(str(path.resolve()), placeholder).replace(str(path), placeholder)
        return _URL_CREDENTIALS.sub("***@", text)

    def conditions(self) -> list[Condition]:
        return [
            Condition(c.condition, c.camera, c.title, self.redact(c.message) or "")
            for c in self._conditions()
        ]

    def _conditions(self) -> list[Condition]:
        """Everything that's wrong right now, from data that already exists."""
        settings = self.settings()
        out: list[Condition] = []
        if settings.primary() is None:
            out.append(
                Condition(
                    "no_destination",
                    None,
                    "No backup destination",
                    "Set a backup destination in Settings; nothing is backed up yet.",
                )
            )
        for cam in settings.cameras:
            if not cam.enabled:
                continue
            label = cam.name or cam.id
            worker = self.workers.get(cam.id)
            status = camera_status(cam.id, self.db, worker.state if worker else None)
            # Finished runs only: the one in progress would resolve the alert
            # at the start of every run and reopen it when it fails again.
            runs = self.db.runs(cam.id, limit=2, finished=True)
            if len(runs) == 2 and all(r.outcome == "error" for r in runs):
                out.append(
                    Condition(
                        "failing",
                        cam.id,
                        f"{label}: backups are failing",
                        f"The last two backups of {label} failed: {runs[0].error}",
                    )
                )
            if worker and worker.state.unreachable_since:
                waited = datetime.now(UTC) - worker.state.unreachable_since
                interval = timedelta(minutes=settings.backup.interval_minutes)
                if waited >= 2 * interval:
                    out.append(
                        Condition(
                            "camera_offline",
                            cam.id,
                            f"{label}: camera unreachable",
                            f"{label} hasn't answered for {int(waited.total_seconds() // 60)} "
                            "minutes. Is it powered and on the network?",
                        )
                    )
            if status.verdict == "at_risk":
                out.append(Condition("at_risk", cam.id, f"{label}: clips at risk", status.text))
            if status.failing_clips:
                out.append(
                    Condition(
                        "clips_keep_failing",
                        cam.id,
                        f"{label}: clips keep failing",
                        f"{status.failing_clips} clips from {label} failed to download "
                        "several times; they are retried once a day.",
                    )
                )
            if worker and worker.health.check_errors:
                out.append(
                    Condition(
                        "check_errors",
                        cam.id,
                        f"{label}: backup check found errors",
                        f"restic check found {worker.health.check_errors} errors in "
                        f"{label}'s repository. Run `restic check` on it and repair it.",
                    )
                )
            if worker and worker.state.index_mismatch:
                out.append(
                    Condition(
                        "index_mismatch",
                        cam.id,
                        f"{label}: backups not found",
                        f"The destination answered with no snapshots for {label}, but clips were "
                        "backed up there. Check that the right destination is configured.",
                    )
                )
        alerts = settings.alerts
        if alerts.rules.enabled and not any(c["configured"] for c in channel_status(alerts)):
            out.append(
                Condition(
                    "no_channel",
                    None,
                    "Alerts have nowhere to go",
                    "Alerts are on, but no channel is set up.",
                )
            )
        out += self._destination_conditions(settings)
        return out

    def _destination_conditions(self, settings: Settings) -> list[Condition]:
        out: list[Condition] = []
        for dest in (d for d in settings.destinations if d.enabled):
            st = status_of(self.db, dest.name)
            role = "primary" if dest is settings.primary() else "copy"
            if int(st.get("failures") or 0) >= 2:
                out.append(
                    Condition(
                        "destination_failing",
                        None,
                        f"{dest.name}: can't be reached",
                        f"The {role} destination {dest.name} has failed {st['failures']} times "
                        f"in a row: {st.get('error') or 'see the log'}.",
                    )
                )
            if role == "copy":
                missing, oldest = lag(self.db, settings, dest)
                seeding = bool(st.get("seeding"))
                if (
                    missing
                    and not seeding
                    and oldest
                    and datetime.now(UTC) - oldest > timedelta(days=1)
                ):
                    out.append(
                        Condition(
                            "copy_behind",
                            None,
                            f"{dest.name}: copy is behind",
                            f"{missing} snapshots haven't been copied to {dest.name}; the oldest "
                            f"is from {oldest:%d %b %H:%M UTC}.",
                        )
                    )
            for cam in settings.cameras if role == "copy" else []:
                bad = int(self.db.get_setting(f"check_errors:{dest.name}:{cam.id}") or 0)
                if bad:
                    out.append(
                        Condition(
                            "check_errors",
                            cam.id,
                            f"{cam.name or cam.id}: {dest.name} check found errors",
                            f"restic check found {bad} errors in {cam.id}'s repository on "
                            f"{dest.name}. Run `restic check` on it and repair it.",
                        )
                    )
            if st.get("too_small"):
                out.append(
                    Condition(
                        "quota_too_small",
                        None,
                        f"{dest.name}: size limit too small",
                        f"Even the newest backups don't fit in {dest.max_gb} GB on {dest.name}. "
                        "Raise the limit.",
                    )
                )
            elif quota_short(st):
                out.append(
                    Condition(
                        "quota_retention_short",
                        None,
                        f"{dest.name}: size limit cut backups short",
                        f"{dest.name} reached its {dest.max_gb} GB limit and deleted backups "
                        f"younger than the {dest.keep_days or 'unlimited'} days you asked for.",
                    )
                )
            if int(st.get("blocked") or 0):
                out.append(
                    Condition(
                        "quota_blocked",
                        None,
                        f"{dest.name}: over its size limit",
                        f"{dest.name} is over {dest.max_gb} GB but {st['blocked']} snapshots can't "
                        "be deleted until they are copied to the other destinations.",
                    )
                )
        return out

    def start(self) -> threading.Event:
        """Start the camera workers, follow config changes, and evaluate
        alerts once a minute. Returns the event that stops it all."""
        self.db.abort_unfinished_runs()
        stop = threading.Event()
        self.config.subscribe(lambda _old, _new: self._on_config_change())
        self.config.watch(stop)
        self.workers.sync()
        self.destinations.sync()
        self.config_backup.start()

        def alerts() -> None:
            while not stop.wait(60):
                self.notifier.evaluate()

        def updates() -> None:
            if stop.wait(30):
                return
            while True:
                if self.settings().web.update_check and (tag := updates_mod.latest_release()):
                    self.db.set_setting("latest_release", tag)
                if stop.wait(86400):
                    return

        threading.Thread(target=alerts, name="alerts", daemon=True).start()
        threading.Thread(target=updates, name="updates", daemon=True).start()
        return stop

    def _on_config_change(self) -> None:
        self.workers.sync()
        self.destinations.sync()
        self.bus.publish("settings", origin=self.config.last_origin)

    def stop(self, stop: threading.Event) -> None:
        stop.set()
        self.workers.shutdown()
        self.destinations.shutdown()
        self.config_backup.shutdown()
        self.gateway.stop()
