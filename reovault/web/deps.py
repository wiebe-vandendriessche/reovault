"""Per-app state and the FastAPI dependencies every `/api/v1` router shares.

`AppState` lives on `app.state.rv`, set once by `create_app`, so routers are
plain module-level `APIRouter`s instead of closures inside one giant
factory function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, HTTPException, Request

from reovault.archiver import Archiver
from reovault.config import Settings
from reovault.db.repository import Repository
from reovault.fleet import Fleet
from reovault.notify import Notifier
from reovault.providers.base import CameraRegistry
from reovault.web.activity import ActivityLog
from reovault.web.events import EventBus
from reovault.web.ratelimit import LoginLimiter
from reovault.web.views import redact_paths


@dataclass
class AppState:
    fleet: Fleet
    settings: Settings
    repo: Repository
    scheduler: BackgroundScheduler | None
    registry: CameraRegistry | None
    activity_log: ActivityLog
    session_key: bytes
    limiter: LoginLimiter
    bus: EventBus
    notifier: Notifier

    def redact(self, text: str | None) -> str | None:
        """Every error string leaving the API goes through this: an
        `OSError` message in `archive_runs.error` or `recordings.last_error`
        can carry an absolute vault/staging path."""
        return redact_paths(text, self.settings.storage) if text else None

    def session_epoch(self) -> int:
        return int(self.repo.get_setting("session_epoch") or 0)


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.rv
    return state


State = Annotated[AppState, Depends(get_state)]


@dataclass(frozen=True, slots=True)
class DeviceCtx:
    archiver: Archiver
    device_id: int
    tz: str


def device_ctx(st: State, device: int | None = None) -> DeviceCtx:
    """The camera a device-scoped endpoint is about: `?device=<id>`, or the
    fleet's sole enabled device when there is exactly one. The dashboard
    remembers the last pick itself and always sends it, so the server needs
    no cookie fallback."""
    if device is None:
        ids = st.fleet.ids()
        if not ids:
            raise HTTPException(409, "No camera is enabled.")
        if len(ids) > 1:
            raise HTTPException(400, "Pick a camera: ?device=<id> is required.")
        device = ids[0]
    archiver = st.fleet.get(device)
    row = st.repo.get_device(device)
    if archiver is None or row is None:
        raise HTTPException(404, "Unknown or disabled camera.")
    return DeviceCtx(archiver=archiver, device_id=device, tz=row.timezone)


Device = Annotated[DeviceCtx, Depends(device_ctx)]
