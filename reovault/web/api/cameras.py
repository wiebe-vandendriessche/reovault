"""The overview, the camera list, adding and editing cameras, run now/stop."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

from reovault import camera as cam_cli
from reovault.config_store import remove_camera_entry, upsert_camera_entry
from reovault.db import Run, parse_iso
from reovault.status import Verdict, camera_status, timeline
from reovault.tools import ToolError
from reovault.web.api.config import write
from reovault.web.api.destinations import DestinationOut, destination_out
from reovault.web.deps import AppState, State, camera_or_404

router = APIRouter(tags=["cameras"])

# kv key: when the user confirmed they stored the restic password elsewhere.
PASSWORD_CONFIRMED = "restic_password_confirmed"

# Alias of the repository that holds ReoVault's own config backup.
RESERVED_ALIAS = "reovault-config"


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


class BucketOut(BaseModel):
    backed_up: int
    waiting: int
    failing: int
    lost: int


class CameraOut(BaseModel):
    alias: str
    name: str | None
    timezone: str
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
    # The SD card in 48 slices, oldest to now; only on the overview.
    timeline: list[BucketOut] | None = None


def camera_out(st: AppState, alias: str, *, with_timeline: bool = False) -> CameraOut:
    cfg = camera_or_404(st, alias)
    worker = st.rt.workers.get(alias)
    state = worker.state if worker else None
    status = camera_status(alias, st.db, state)
    clips, size = st.db.totals(alias)
    runs = st.db.runs(alias, limit=1)
    card = state.card if state else None
    return CameraOut(
        alias=alias,
        name=cfg.name,
        timezone=cfg.timezone,
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
        timeline=[BucketOut(**asdict(b)) for b in timeline(alias, st.db, state)]
        if with_timeline
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


@router.get("/overview")
def overview(st: State) -> OverviewOut:
    """Read from the index and memory only: loading it never calls a camera
    or the destination."""
    cams = [camera_out(st, c.alias, with_timeline=True) for c in st.settings.cameras if c.enabled]
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
    )


@router.get("/cameras")
def list_cameras(st: State) -> list[CameraOut]:
    return [camera_out(st, c.alias) for c in st.settings.cameras]


@router.post("/cameras/{alias}/run", responses={409: {}})
def run_now(alias: str, st: State) -> CameraOut:
    worker = st.rt.workers.get(camera_or_404(st, alias).alias)
    if worker is None:
        raise HTTPException(409, "This camera is turned off.")
    worker.run_now()
    return camera_out(st, alias)


@router.post("/cameras/{alias}/stop", responses={409: {}})
def stop(alias: str, st: State) -> CameraOut:
    """Stops after the batch in flight; nothing finished is undone."""
    worker = st.rt.workers.get(camera_or_404(st, alias).alias)
    if worker is None or not worker.running:
        raise HTTPException(409, "No backup is running for this camera.")
    worker.stop_run()
    return camera_out(st, alias)


class DiscoveredOut(BaseModel):
    host: str
    uid: str | None
    model: str | None
    name: str | None
    already_registered: bool


@router.post("/cameras/discover", responses={502: {}})
def discover(st: State) -> list[DiscoveredOut]:
    try:
        found = cam_cli.discover(st.settings.tools.reolink_cli)
    except ToolError as exc:
        raise HTTPException(502, st.redact(str(exc))) from None
    return [DiscoveredOut(**d.__dict__) for d in found] if found else []


class AddCameraIn(BaseModel):
    alias: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str | None = Field(default=None, max_length=100)
    timezone: str
    demo: bool = False
    # Only for a real camera; goes straight into reolink-cli's encrypted registry.
    host: str | None = Field(default=None, max_length=255)
    user: str | None = Field(default=None, max_length=64)
    password: SecretStr | None = Field(default=None, max_length=256)


def _valid_tz(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - every zoneinfo failure is the same user error
        raise HTTPException(422, f"Unknown timezone: {tz!r}") from None


@router.post("/cameras", status_code=201, responses={409: {}, 422: {}, 502: {}})
def add_camera(body: AddCameraIn, st: State) -> list[CameraOut]:
    _valid_tz(body.timezone)
    if body.alias == RESERVED_ALIAS:
        raise HTTPException(
            422, f"{RESERVED_ALIAS!r} is reserved for ReoVault's own config backup."
        )
    if not st.rt.config.writable:
        # Checked before reolink-cli is touched, so a refusal never leaves a
        # camera registered there but missing here.
        raise HTTPException(409, "The config file is read-only, so cameras can't be added here.")
    if st.settings.camera(body.alias) is not None:
        raise HTTPException(409, f"{body.alias!r} already exists.")
    if not body.demo:
        if not (body.host and body.user and body.password):
            raise HTTPException(422, "A camera needs its address, user name and password.")
        try:
            if body.alias in cam_cli.registered_aliases(st.settings.tools.reolink_cli):
                raise HTTPException(409, f"{body.alias!r} is already registered in reolink-cli.")
            cam_cli.add(
                st.settings.tools.reolink_cli,
                alias=body.alias,
                host=body.host,
                user=body.user,
                password=body.password,
            )
        except ToolError as exc:
            raise HTTPException(502, f"Could not add the camera: {st.redact(str(exc))}") from None
    write(
        st,
        lambda doc: upsert_camera_entry(
            doc,
            body.alias,
            timezone=body.timezone,
            name=body.name or None,
            demo=True if body.demo else None,
        ),
    )
    return list_cameras(st)


@router.delete("/cameras/{alias}", responses={404: {}, 409: {}, 502: {}})
def remove_camera(alias: str, st: State, registry: bool = False) -> list[CameraOut]:
    """Stops backing a camera up and drops it from reovault.toml. What is
    already backed up stays in every destination, and re-adding the same alias
    brings it all back. With `registry`, a real camera is also removed from
    reolink-cli's registry (its stored password goes with it)."""
    cfg = camera_or_404(st, alias)
    if not st.rt.config.writable:
        raise HTTPException(409, "The config file is read-only, so cameras can't be removed here.")
    if registry and not cfg.demo:
        try:
            cam_cli.remove(st.settings.tools.reolink_cli, alias)
        except ToolError as exc:
            raise HTTPException(
                502, f"Could not remove it from reolink-cli: {st.redact(str(exc))}"
            ) from None
    write(st, lambda doc: remove_camera_entry(doc, alias))
    return list_cameras(st)


class EditCameraIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    timezone: str
    enabled: bool


@router.put("/cameras/{alias}", responses={409: {}, 422: {}})
def edit_camera(alias: str, body: EditCameraIn, st: State) -> list[CameraOut]:
    camera_or_404(st, alias)
    _valid_tz(body.timezone)
    write(
        st,
        lambda doc: upsert_camera_entry(
            doc,
            alias,
            name=body.name or None,
            timezone=body.timezone,
            enabled=None if body.enabled else False,  # true is the default
        ),
    )
    return list_cameras(st)
