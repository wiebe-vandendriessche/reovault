"""The running app: settings, database, workers and alerts, shared by the
daemon and the web API."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from reovault.alerts import Condition, Notifier, channel_status
from reovault.camera import Gateway
from reovault.config import Settings
from reovault.config_store import ConfigStore
from reovault.db import Db
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
    notifier: Notifier = field(init=False)

    def __post_init__(self) -> None:
        self.workers = Workers(self.settings, self.db, self.gateway, self.bus.publish)
        self.notifier = Notifier(
            settings=self.settings, db=self.db, conditions=self.conditions, publish=self.bus.publish
        )

    def settings(self) -> Settings:
        return self.config.current

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
        if not settings.backup.repository:
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
            label = cam.name or cam.alias
            worker = self.workers.get(cam.alias)
            status = camera_status(cam.alias, self.db, worker.state if worker else None)
            runs = self.db.runs(cam.alias, limit=2)
            if len(runs) == 2 and all(r.outcome == "error" for r in runs):
                out.append(
                    Condition(
                        "failing",
                        cam.alias,
                        f"{label}: backups are failing",
                        f"The last two backups of {label} failed: {runs[0].error}",
                    )
                )
            if status.verdict == "at_risk":
                out.append(Condition("at_risk", cam.alias, f"{label}: clips at risk", status.text))
            if status.failing_clips:
                out.append(
                    Condition(
                        "clips_keep_failing",
                        cam.alias,
                        f"{label}: clips keep failing",
                        f"{status.failing_clips} clips from {label} failed to download "
                        "several times; they are retried once a day.",
                    )
                )
            if worker and worker.health.check_errors:
                out.append(
                    Condition(
                        "check_errors",
                        cam.alias,
                        f"{label}: backup check found errors",
                        f"restic check found {worker.health.check_errors} errors in "
                        f"{label}'s repository. Run `restic check` on it and repair it.",
                    )
                )
            if worker and worker.health.index_mismatch:
                out.append(
                    Condition(
                        "index_mismatch",
                        cam.alias,
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
        return out

    def start(self) -> threading.Event:
        """Start the camera workers, follow config changes, and evaluate
        alerts once a minute. Returns the event that stops it all."""
        self.db.abort_unfinished_runs()
        stop = threading.Event()
        self.config.subscribe(lambda _old, _new: self._on_config_change())
        self.config.watch(stop)
        self.workers.sync()

        def alerts() -> None:
            while not stop.wait(60):
                self.notifier.evaluate()

        threading.Thread(target=alerts, name="alerts", daemon=True).start()
        return stop

    def _on_config_change(self) -> None:
        self.workers.sync()
        self.bus.publish("settings", origin=self.config.last_origin)

    def stop(self, stop: threading.Event) -> None:
        stop.set()
        self.workers.shutdown()
        self.gateway.stop()
