"""The running app: settings, database, workers and alerts, shared by the
daemon and the web API."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reovault import tools
from reovault import updates as updates_mod
from reovault.alerts import Condition, Notifier, channel_status
from reovault.camera import Gateway
from reovault.config import Settings
from reovault.config_store import ConfigStore
from reovault.configbackup import ConfigBackup
from reovault.db import Db, parse_iso
from reovault.destinations import Destinations, lag, quota_short, status_of
from reovault.status import camera_status
from reovault.web.events import EventBus
from reovault.worker import Workers

# No successful backup for this long (or three intervals, if longer) is its own
# alert: it says how long footage has only existed on the SD card.
STALE_AFTER = timedelta(hours=6)

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
        return [replace(c, message=self.redact(c.message) or "") for c in self._conditions()]

    def _conditions(self) -> list[Condition]:
        """Everything that's wrong right now, from data that already exists.
        Each condition clears by itself once the cause is gone, or names the
        dashboard action that resolves it."""
        settings = self.settings()
        out: list[Condition] = []
        if settings.primary() is None:
            out.append(
                Condition(
                    "no_destination",
                    None,
                    "No backup destination",
                    "Set a backup destination in Settings; nothing is backed up yet.",
                    actions=("destinations",),
                )
            )
        now = datetime.now(UTC)
        primary = settings.primary()
        down = self._down_since(primary.name) if primary else None
        if primary and down:
            out.append(
                Condition(
                    "destination_unreachable",
                    None,
                    f"{primary.name} can't be reached",
                    f"ReoVault hasn't reached {primary.name} since {down:%d %b %H:%M} UTC. "
                    "Nothing needs doing on this side: backups resume on their own once it "
                    "answers again. Until then new clips wait on the SD cards; if any get "
                    "close to being overwritten, that is a separate alert.",
                    subject=primary.name,
                    actions=("destinations",),
                )
            )
        for cam in settings.cameras:
            if not cam.enabled:
                continue
            out += self._camera_conditions(
                settings, cam.id, cam.name or cam.id, now, primary_down=down is not None
            )
        alerts = settings.alerts
        if alerts.rules.enabled and not any(c["configured"] for c in channel_status(alerts)):
            out.append(
                Condition(
                    "no_channel",
                    None,
                    "Alerts have nowhere to go",
                    "Alerts are on, but no channel is set up.",
                    severity="warning",
                    actions=("alerts",),
                )
            )
        out += self._destination_conditions(settings)
        return out

    def _down_since(self, destination: str) -> datetime | None:
        raw = self.db.get_setting(f"down_since:{destination}")
        return parse_iso(raw) if raw else None

    def _camera_conditions(
        self,
        settings: Settings,
        cam_id: str,
        label: str,
        now: datetime,
        *,
        primary_down: bool = False,
    ) -> list[Condition]:
        """`primary_down`: the destination outage is reported once on its own;
        a camera's failing, stale and upkeep alerts would only repeat it."""
        out: list[Condition] = []
        worker = self.workers.get(cam_id)
        state = worker.state if worker else None
        status = camera_status(cam_id, self.db, state)
        interval = timedelta(minutes=settings.backup.interval_minutes)
        # Finished runs that decided something: the one in progress would
        # resolve the alert at the start of every run, and a stopped one (a
        # restart, the Stop button, the size-limit hold) proves nothing.
        runs = self.db.runs(cam_id, limit=2, finished=True, decided=True)
        last_ok = self.db.last_ok(cam_id)
        stale_after = max(STALE_AFTER, 3 * interval)
        if primary_down:
            pass
        elif last_ok is not None and now - last_ok >= stale_after:
            error = next((r.error for r in runs if r.outcome == "error"), None)
            out.append(
                Condition(
                    "stale",
                    cam_id,
                    f"{label}: no backup since {last_ok:%d %b %H:%M} UTC",
                    f"{label} hasn't had a successful backup for "
                    f"{_hours(now - last_ok)}, so new clips only exist on its SD card."
                    + (f" The last error: {error}" if error else ""),
                    actions=("run",),
                )
            )
        elif len(runs) == 2 and all(r.outcome == "error" for r in runs):
            out.append(
                Condition(
                    "failing",
                    cam_id,
                    f"{label}: backups are failing",
                    f"The last two backups of {label} failed: {runs[0].error}",
                    actions=("run",),
                )
            )
        if state and state.held:
            out.append(
                Condition(
                    "held",
                    cam_id,
                    f"{label}: backups are waiting",
                    state.held,
                    severity="warning",
                    actions=("destinations",),
                )
            )
        if state and state.unreachable_since:
            waited = now - state.unreachable_since
            if waited >= 2 * interval:
                out.append(
                    Condition(
                        "camera_offline",
                        cam_id,
                        f"{label}: camera unreachable",
                        f"{label} hasn't answered for {int(waited.total_seconds() // 60)} "
                        "minutes. Is it powered and on the network?",
                    )
                )
        if status.verdict == "at_risk":
            out.append(
                Condition(
                    "at_risk", cam_id, f"{label}: clips at risk", status.text, actions=("run",)
                )
            )
        if status.failing_clips:
            out.append(
                Condition(
                    "clips_keep_failing",
                    cam_id,
                    f"{label}: clips keep failing",
                    f"{status.failing_clips} clips from {label} failed to download several "
                    "times. They are retried once a day until the SD card overwrites them; "
                    "retry them now or give up on them.",
                    severity="warning",
                    actions=("retry_clips", "give_up_clips"),
                )
            )
        # A check turned off says nothing current: its last findings aren't alerted.
        for dest in (d for d in settings.destinations if d.enabled and d.check):
            tag = f"{dest.name}:{cam_id}"
            bad = int(self.db.get_setting(f"check_errors:{tag}") or 0)
            if bad:
                detail = self.db.get_setting(f"check_detail:{tag}") or ""
                out.append(
                    Condition(
                        "check_errors",
                        cam_id,
                        f"{label}: {dest.name} check found damage",
                        f"restic check found {bad} errors in {label}'s repository on "
                        f"{dest.name}, twice in a row. {detail} Repair it (see "
                        "docs/restore-without-reovault.md), then check again.",
                        subject=dest.name,
                        actions=("check",),
                    )
                )
        if worker and worker.health.maintenance_failures >= 2 and not primary_down:
            out.append(
                Condition(
                    "maintenance",
                    cam_id,
                    f"{label}: upkeep is failing",
                    f"Retention or the weekly check for {label} failed "
                    f"{worker.health.maintenance_failures} times in a row: "
                    f"{worker.health.maintenance_error}",
                    severity="warning",
                    actions=("check",),
                )
            )
        if state and state.index_mismatch:
            out.append(
                Condition(
                    "index_mismatch",
                    cam_id,
                    f"{label}: backups not found",
                    f"The destination answered with no snapshots for {label}, but clips were "
                    "backed up there. Check that the right destination is configured.",
                    actions=("destinations",),
                )
            )
        return out

    def _destination_conditions(self, settings: Settings) -> list[Condition]:
        out: list[Condition] = []
        primary = settings.primary()
        primary_down = bool(primary and self._down_since(primary.name))
        for dest in (d for d in settings.destinations if d.enabled):
            st = status_of(self.db, dest.name)
            role = "primary" if dest is primary else "copy"
            down = self._down_since(dest.name) is not None
            # A copy can't receive anything while the primary is unreachable:
            # that outage is the one problem to report.
            if int(st.get("failures") or 0) >= 2 and not (role == "copy" and primary_down):
                out.append(
                    Condition(
                        "destination_failing",
                        None,
                        f"{dest.name}: can't be reached",
                        f"The {role} destination {dest.name} has failed {st['failures']} times "
                        f"in a row: {st.get('error') or 'see the log'}.",
                        subject=dest.name,
                        actions=("destinations",),
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
                            subject=dest.name,
                            severity="warning",
                            actions=("destinations",),
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
                        subject=dest.name,
                        actions=("destinations",),
                    )
                )
            elif quota_short(st, dest):
                out.append(
                    Condition(
                        "quota_retention_short",
                        None,
                        f"{dest.name}: size limit cut backups short",
                        f"{dest.name} reached its {dest.max_gb} GB limit and deleted backups "
                        f"younger than the {dest.keep_days or 'unlimited'} days you asked for. "
                        "Raise the size limit or keep fewer days; either clears this.",
                        subject=dest.name,
                        severity="warning",
                        actions=("destinations",),
                    )
                )
            if st.get("config_error") and not down:
                out.append(
                    Condition(
                        "config_backup",
                        None,
                        f"{dest.name}: settings backup failing",
                        f"ReoVault couldn't back up its own settings to {dest.name}: "
                        f"{st['config_error']}. It tries again every hour.",
                        subject=dest.name,
                        severity="warning",
                        actions=("recovery",),
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
                        subject=dest.name,
                        actions=("destinations",),
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
        tools.interrupt_all()  # restic removes its locks, the workers' calls return
        self.workers.shutdown()
        self.destinations.shutdown()
        self.config_backup.shutdown()
        self.gateway.stop()


def _hours(span: timedelta) -> str:
    hours = int(span.total_seconds() // 3600)
    return f"{hours // 24} days and {hours % 24} hours" if hours >= 48 else f"{hours} hours"
