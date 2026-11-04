"""Per-app state and the dependencies every `/api/v1` router shares."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from reovault.config import CameraConfig, Settings
from reovault.config_store import ConfigStore
from reovault.db import Db
from reovault.runtime import Runtime
from reovault.web.events import EventBus
from reovault.web.ratelimit import LoginLimiter


@dataclass
class AppState:
    rt: Runtime
    session_key: bytes
    limiter: LoginLimiter
    # First-run only: the one-time code printed in the server log, and the lock
    # that lets exactly one browser finish setup.
    setup_code: str | None = None
    setup_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def settings(self) -> Settings:
        return self.rt.settings()

    @property
    def db(self) -> Db:
        return self.rt.db

    @property
    def config(self) -> ConfigStore:
        return self.rt.config

    @property
    def bus(self) -> EventBus:
        return self.rt.bus

    def redact(self, text: str | None) -> str | None:
        return self.rt.redact(text)

    def session_epoch(self) -> int:
        return int(self.db.get_setting("session_epoch") or 0)


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.rv
    return state


State = Annotated[AppState, Depends(get_state)]


def camera_or_404(st: AppState, cam_id: str) -> CameraConfig:
    cam = st.settings.camera(cam_id)
    if cam is None:
        raise HTTPException(404, "Unknown camera.")
    return cam
