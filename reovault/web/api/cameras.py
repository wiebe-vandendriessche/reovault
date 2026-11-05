"""The overview, the camera list, adding and editing cameras, run now/stop."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr

from reovault import camera as cam_cli
from reovault.backup import CameraState
from reovault.config_store import remove_camera_entry, upsert_camera_entry
from reovault.db import Run, parse_iso
from reovault.status import Verdict, camera_status, coverage, coverage_start
from reovault.tools import ToolError
from reovault.web.api.config import write
from reovault.web.api.destinations import DestinationOut, destination_out
from reovault.web.deps import AppState, State, camera_or_404
from reovault.worker import camera_for

router = APIRouter(tags=["cameras"])

# kv key: when the user confirmed they stored the restic password elsewhere.
PASSWORD_CONFIRMED = "restic_password_confirmed"

# The repository that holds ReoVault's own config backup sits next to the cameras' ones.
RESERVED_ID = "reovault-config"


class RunOut(BaseModel):
    id: int
    camera: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    clips: int
    bytes: int
    lost: int
    error: str | None


def run_out(st: AppState, r: Run) -> RunOut:
    return RunOut(
        id=r.id,
        camera=r.camera,
        trigger=r.trigger,
        started_at=parse_iso(r.started_at),
        finished_at=parse_iso(r.finished_at) if r.finished_at else None,
        outcome=r.outcome,
        clips=r.clips,
        bytes=r.bytes,
        lost=r.lost,
        error=st.redact(r.error),
    )


class CardOut(BaseModel):
    total_gb: float | None
    free_gb: float | None
    mounted: bool | None


class RunSegOut(BaseModel):
    start: int  # first bucket
    length: int
    state: Literal["none", "safe", "waiting", "lost", "failing"]
    clips: int


class CoverageRowOut(BaseModel):
    kind: Literal["card", "primary", "copy"]
    name: str
    runs: list[RunSegOut]
    marker: float | None  # percent along the axis: the card's oldest clip


class CoverageOut(BaseModel):
    start: datetime
    end: datetime
    buckets: int
    rows: list[CoverageRowOut]


class CameraOut(BaseModel):
    id: str
    name: str | None
    timezone: str
    host: str | None  # never the password
    user: str
    enabled: bool
    demo: bool
    verdict: Verdict
    text: str
    pending: int
    pending_bytes: int
    eta_seconds: float | None
    oldest_pending_utc: datetime | None
    oldest_on_card_utc: datetime | None
    card: CardOut | None
    lost_7d: int
    failing_clips: int
    backed_up_clips: int
    backed_up_bytes: int
    running: bool
    next_run_at: datetime | None
    last_run: RunOut | None
    # Where the footage of the chosen span is; only on the overview.
    coverage: CoverageOut | None = None


def span_start(st: AppState, cam_id: str, state: CameraState | None, span: str | None) -> datetime:
    return coverage_start(span or "card", cam_id, st.db, state, datetime.now(UTC))


def camera_out(st: AppState, cam_id: str, *, coverage_span: str | None = None) -> CameraOut:
    cfg = camera_or_404(st, cam_id)
    worker = st.rt.workers.get(cam_id)
    state = worker.state if worker else None
    status = camera_status(cam_id, st.db, state)
    clips, size = st.db.totals(cam_id)
    runs = st.db.runs(cam_id, limit=1)
    card = state.card if state else None
    return CameraOut(
        id=cam_id,
        name=cfg.name,
        timezone=cfg.timezone,
        host=cfg.host,
        user=cfg.user,
        enabled=cfg.enabled,
        demo=cfg.demo,
        verdict=status.verdict,
        text=st.redact(status.text) or "",
        pending=status.pending,
        pending_bytes=status.pending_bytes,
        eta_seconds=status.eta_seconds,
        oldest_pending_utc=status.oldest_pending_utc,
        oldest_on_card_utc=status.oldest_on_card_utc,
        card=CardOut(total_gb=card.total_gb, free_gb=card.free_gb, mounted=card.mounted)
        if card
        else None,
        lost_7d=status.lost_7d,
        failing_clips=status.failing_clips,
        backed_up_clips=clips,
        backed_up_bytes=size,
        running=bool(worker and worker.running),
        next_run_at=worker.next_run_at if worker else None,
        last_run=run_out(st, runs[0]) if runs else None,
        coverage=CoverageOut.model_validate(
            asdict(
                coverage(
                    cam_id, st.db, st.settings, state, span_start(st, cam_id, state, coverage_span)
                )
            )
        )
        if coverage_span
        else None,
    )


class IssueOut(BaseModel):
    camera: str | None
    title: str
    message: str


class OverviewOut(BaseModel):
    issues: list[IssueOut]
    # Worth doing, but nothing is wrong: not alerted on.
    notices: list[IssueOut]
    cameras: list[CameraOut]
    destinations: list[DestinationOut]
    # How far back there is anything at all, to offer sensible spans.
    oldest_backup: datetime | None


_SPAN = r"^(card|all|[1-9][0-9]{0,4})$"


@router.get("/overview", responses={422: {}})
def overview(st: State, span: Annotated[str, Query(pattern=_SPAN)] = "card") -> OverviewOut:
    """Read from the index and memory only: loading it never calls a camera
    or the destination. `span` is how far back each camera's coverage looks:
    `card` (since its SD card starts), `all` (everything ReoVault knows), or a
    number of days."""
    cams = [camera_out(st, c.id, coverage_span=span) for c in st.settings.cameras if c.enabled]
    notices = []
    if any(c.backed_up_clips for c in cams) and st.db.get_setting(PASSWORD_CONFIRMED) is None:
        notices.append(
            IssueOut(
                camera=None,
                title="Keep your restic password safe",
                message="Every backup is unreadable without it. Store it somewhere other than this "
                "server, then confirm on the Recovery tab in Settings.",
            )
        )
    return OverviewOut(
        issues=[
            IssueOut(camera=c.camera, title=c.title, message=c.message) for c in st.rt.conditions()
        ],
        notices=notices,
        cameras=cams,
        destinations=[destination_out(st, d) for d in st.settings.destinations],
        oldest_backup=st.db.oldest_kept(),
    )


@router.get("/cameras")
def list_cameras(st: State) -> list[CameraOut]:
    return [camera_out(st, c.id) for c in st.settings.cameras]


@router.post("/cameras/{cam_id}/run", responses={409: {}})
def run_now(cam_id: str, st: State) -> CameraOut:
    worker = st.rt.workers.get(camera_or_404(st, cam_id).id)
    if worker is None:
        raise HTTPException(409, "This camera is turned off.")
    worker.run_now()
    return camera_out(st, cam_id)


@router.post("/cameras/{cam_id}/stop", responses={409: {}})
def stop(cam_id: str, st: State) -> CameraOut:
    """Stops after the batch in flight; nothing finished is undone."""
    worker = st.rt.workers.get(camera_or_404(st, cam_id).id)
    if worker is None or not worker.running:
        raise HTTPException(409, "No backup is running for this camera.")
    worker.stop_run()
    return camera_out(st, cam_id)


class DeviceOut(BaseModel):
    model: str | None
    firmware: str | None
    hardware: str | None
    card: CardOut | None
    checked_at: datetime
    error: str | None  # the camera couldn't be asked; everything else is None


_DEVICE_TTL = timedelta(minutes=10)
# ponytail: process-wide, per (camera, address, user); good enough for a handful
# of cameras on one page.
_devices: dict[tuple[str, str | None, str], DeviceOut] = {}


@router.get("/cameras/{cam_id}/device", responses={404: {}})
def device(cam_id: str, st: State) -> DeviceOut:
    """The camera itself: model, firmware and the SD card, asked live (cached
    for 10 minutes, so opening the page doesn't keep calling the camera)."""
    cfg = camera_or_404(st, cam_id)
    key = (cam_id, cfg.host, cfg.user)
    now = datetime.now(UTC)
    hit = _devices.get(key)
    if hit is not None and now - hit.checked_at < _DEVICE_TTL:
        return hit
    cam = camera_for(st.settings, cfg, st.rt.gateway)
    try:
        info, card = cam.info(), cam.storage()
    except ToolError as exc:
        return DeviceOut(
            model=None, firmware=None, hardware=None, card=None, checked_at=now,
            error=f"Can't reach the camera: {st.redact(str(exc))}",
        )  # fmt: skip
    out = DeviceOut(
        model=info.model, firmware=info.firmware, hardware=info.hardware,
        card=CardOut(total_gb=card.total_gb, free_gb=card.free_gb, mounted=card.mounted),
        checked_at=now, error=None,
    )  # fmt: skip
    _devices[key] = out
    return out


class DiscoveredOut(BaseModel):
    host: str
    uid: str | None
    model: str | None
    name: str | None
    added: bool


@router.post("/cameras/discover", responses={502: {}})
def discover(st: State) -> list[DiscoveredOut]:
    known = {c.host for c in st.settings.cameras if c.host}
    try:
        found = cam_cli.discover(st.settings.tools.reolink_cli, known)
    except ToolError as exc:
        raise HTTPException(502, st.redact(str(exc))) from None
    return [DiscoveredOut(**d.__dict__) for d in found]


_HOST = Field(default=None, max_length=255, pattern=r"^[A-Za-z0-9.:\[\]-]+$")


class AddCameraIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str | None = Field(default=None, max_length=100)
    timezone: str
    demo: bool = False
    # Only for a real camera; written to reovault.toml, never sent back.
    host: str | None = _HOST
    user: str = Field(default="admin", min_length=1, max_length=64)
    password: SecretStr | None = Field(default=None, max_length=256)


def _valid_tz(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - every zoneinfo failure is the same user error
        raise HTTPException(422, f"Unknown timezone: {tz!r}") from None


def _check_login(st: AppState, host: str, user: str, password: SecretStr) -> None:
    """Log in once before saving, so a typo shows up now, not at the first backup."""
    try:
        cam_cli.check(
            st.settings.tools.reolink_cli, st.rt.gateway, host=host, user=user, password=password
        )
    except ToolError as exc:
        raise HTTPException(502, f"Could not reach the camera: {st.redact(str(exc))}") from None


@router.post("/cameras", status_code=201, responses={409: {}, 422: {}, 502: {}})
def add_camera(body: AddCameraIn, st: State) -> list[CameraOut]:
    _valid_tz(body.timezone)
    if body.id == RESERVED_ID:
        raise HTTPException(422, f"{RESERVED_ID!r} is reserved for ReoVault's own config backup.")
    if not st.rt.config.writable:
        raise HTTPException(409, "The config file is read-only, so cameras can't be added here.")
    if st.settings.camera(body.id) is not None:
        raise HTTPException(409, f"{body.id!r} already exists.")
    if not body.demo:
        if not (body.host and body.password):
            raise HTTPException(422, "A camera needs its address and password.")
        _check_login(st, body.host, body.user, body.password)
    write(
        st,
        lambda doc: upsert_camera_entry(
            doc,
            body.id,
            name=body.name or None,
            timezone=body.timezone,
            host=None if body.demo else body.host,
            user=None if body.demo or body.user == "admin" else body.user,
            password=None
            if body.demo or body.password is None
            else body.password.get_secret_value(),
            demo=True if body.demo else None,
        ),
    )
    return list_cameras(st)


@router.delete("/cameras/{cam_id}", responses={404: {}, 409: {}})
def remove_camera(cam_id: str, st: State) -> list[CameraOut]:
    """Stops backing a camera up and drops it (with its password) from
    reovault.toml. What is already backed up stays in every destination, and
    adding a camera with the same id again brings it all back."""
    camera_or_404(st, cam_id)
    if not st.rt.config.writable:
        raise HTTPException(409, "The config file is read-only, so cameras can't be removed here.")
    write(st, lambda doc: remove_camera_entry(doc, cam_id))
    return list_cameras(st)


class EditCameraIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    timezone: str
    enabled: bool
    host: str | None = _HOST
    user: str | None = Field(default=None, min_length=1, max_length=64)
    password: SecretStr | None = Field(default=None, max_length=256)  # None keeps it


@router.put("/cameras/{cam_id}", responses={409: {}, 422: {}, 502: {}})
def edit_camera(cam_id: str, body: EditCameraIn, st: State) -> list[CameraOut]:
    cfg = camera_or_404(st, cam_id)
    _valid_tz(body.timezone)
    login: dict[str, str | None] = {}
    if not cfg.demo:
        host, user = body.host or cfg.host, body.user or cfg.user
        assert host is not None
        if (host, user) != (cfg.host, cfg.user) and body.password is None:
            # Never send the stored password to a new address: a stolen session
            # could point the camera at its own host and capture the login.
            raise HTTPException(
                422, "Enter the camera password again to change its address or user."
            )
        if body.password is not None:
            _check_login(st, host, user, body.password)
        login = {
            "host": host,
            "user": None if user == "admin" else user,
            **({"password": body.password.get_secret_value()} if body.password else {}),
        }
    write(
        st,
        lambda doc: upsert_camera_entry(
            doc,
            cam_id,
            name=body.name or None,
            timezone=body.timezone,
            enabled=None if body.enabled else False,  # true is the default
            **login,
        ),
    )
    return list_cameras(st)
