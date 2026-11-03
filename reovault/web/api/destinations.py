"""Destinations: where backups go. List them with their live status, connect a
new one (a local folder or an rclone remote), change its limits, switch which
one is primary, disconnect one. Everything is written to reovault.toml; the
running app follows (see `Destinations.sync`)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from tomlkit.toml_document import TOMLDocument

from reovault import rclone, retention
from reovault.config import DestinationConfig
from reovault.config_store import remove_destination_entry, upsert_destination_entry
from reovault.db import parse_iso
from reovault.destinations import lag, quota_short, restic_for, status_of, usage
from reovault.tools import ToolError
from reovault.web.api.auth import confirm_password
from reovault.web.api.config import write
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/destinations", tags=["destinations"])


class SeedingOut(BaseModel):
    camera: str
    done: int
    total: int


class DestinationOut(BaseModel):
    name: str
    repository: str  # credentials scrubbed
    role: str  # primary | copy | off
    keep_days: int
    max_gb: int
    used_bytes: int  # about: the footage in its snapshots
    snapshots: int
    seeding: list[SeedingOut]
    error: str | None
    last_ok: datetime | None
    quota_short: bool  # the quota cut retention short in the last week
    quota_too_small: bool  # even the newest snapshots don't fit
    held_back: int  # snapshots the quota wants gone but a copy doesn't have yet
    behind: bool  # a copy more than a day behind, and not simply seeding


def destination_out(st: AppState, d: DestinationConfig) -> DestinationOut:
    s = status_of(st.db, d.name)
    primary = st.settings.primary()
    used = usage(st.db, d.name)
    seeding = [
        SeedingOut(camera=cam, done=v[0], total=v[1]) for cam, v in (s.get("seeding") or {}).items()
    ]
    last_ok = parse_iso(s["last_ok"]) if s.get("last_ok") else None
    role = "off" if not d.enabled else "primary" if d is primary else "copy"
    return DestinationOut(
        name=d.name,
        repository=st.rt.scrub_credentials(d.repository) or "",
        role=role,
        keep_days=d.keep_days,
        max_gb=d.max_gb,
        used_bytes=sum(u.bytes for u in used),
        snapshots=len(used),
        seeding=seeding,
        error=st.redact(s.get("error")),
        last_ok=last_ok,
        quota_short=quota_short(s),
        quota_too_small=bool(s.get("too_small")),
        held_back=int(s.get("blocked") or 0),
        behind=role == "copy" and not seeding and bool(s.get("failures")),
    )


def _all(st: AppState) -> list[DestinationOut]:
    return [destination_out(st, d) for d in st.settings.destinations]


def _dest_or_404(st: AppState, name: str) -> DestinationConfig:
    d = st.settings.destination(name)
    if d is None:
        raise HTTPException(404, "Unknown destination.")
    return d


@router.get("")
def list_destinations(st: State) -> list[DestinationOut]:
    return _all(st)


class RemoteFieldOut(BaseModel):
    key: str
    label: str
    secret: bool
    required: bool
    default: str
    help: str


class KindOut(BaseModel):
    id: str
    label: str
    paste: bool
    help: str
    fields: list[RemoteFieldOut]


@router.get("/kinds")
def kinds() -> list[KindOut]:
    """What the "connect" form offers, so the dashboard doesn't hard-code it."""
    return [
        KindOut(
            id=k.id,
            label=k.label,
            paste=k.paste,
            help=k.help,
            fields=[
                RemoteFieldOut(key=f.key, label=f.label, secret=f.secret, required=f.required,
                         default=f.default, help=f.help)
                for f in k.fields
            ],
        )
        for k in rclone.TYPES
    ]  # fmt: skip


class ConnectIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    kind: str  # "local" or an rclone kind id
    path: str = Field(default="", max_length=1024)  # a local folder, or the folder on the remote
    values: dict[str, str] = Field(default_factory=dict)
    keep_days: int = Field(default=0, ge=0, le=36500)
    max_gb: int = Field(default=0, ge=0, le=10_000_000)


def _check_local(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(422, "Use the full path of the folder, starting with /.")
    if not p.is_dir():
        raise HTTPException(
            422,
            f"{path} doesn't exist here. In Docker, mount the disk into the container first: "
            "a folder that isn't there is usually an unmounted disk.",
        )
    if not os.access(p, os.W_OK):
        raise HTTPException(422, f"{path} isn't writable by ReoVault.")
    return str(p)


def _remote_path(path: str) -> str:
    return path.strip().strip("/") or "reovault"


@router.post("", status_code=201, responses={409: {}, 422: {}, 502: {}})
def connect(body: ConnectIn, st: State) -> list[DestinationOut]:
    """Connect a destination. It joins as a copy (or as the primary when it's the
    first); `Make primary` is a separate, confirmed step."""
    if not st.rt.config.writable:
        raise HTTPException(
            409, "The config file is read-only, so destinations can't be added here."
        )
    if not rclone.valid_name(body.name):
        raise HTTPException(
            422, "A name is letters, digits, - and _ (up to 32), starting with one."
        )
    if st.settings.destination(body.name) is not None:
        raise HTTPException(409, f"A destination named {body.name!r} already exists.")
    tools = st.settings.tools
    config = os.environ.get("RCLONE_CONFIG")
    created: str | None = None
    try:
        if body.kind == "local":
            repository = _check_local(body.path)
        else:
            kind = rclone.BY_ID.get(body.kind)
            if kind is None:
                raise HTTPException(422, "Unknown destination type.")
            created = rclone.create(tools.rclone, body.name, kind, body.values, config)
            rclone.check(tools.rclone, created, config)
            repository = f"rclone:{created}:{_remote_path(body.path)}"
    except ToolError as exc:
        if created:
            rclone.delete(tools.rclone, created, config)  # a half-made remote is no use
        raise HTTPException(422, st.redact(str(exc))) from None
    except HTTPException:
        if created:
            rclone.delete(tools.rclone, created, config)
        raise
    current = st.settings.primary()
    # A name reused for a different repository must not inherit what was
    # remembered about the old one (its snapshots, "the repository existed").
    remembered = st.db.get_setting(f"repository_of:{body.name}")
    if remembered is not None and remembered != repository:
        st.db.forget_destination(body.name)
    st.db.set_setting(f"repository_of:{body.name}", repository)

    def mutate(doc: TOMLDocument) -> None:
        if current is not None:
            # A lone destination is primary only implicitly; with a second one
            # the config must say which.
            upsert_destination_entry(doc, current.name, primary=True)
        upsert_destination_entry(
            doc, body.name, repository=repository, primary=True if current is None else None,
            keep_days=body.keep_days or None, max_gb=body.max_gb or None,
        )  # fmt: skip

    try:
        write(st, mutate)
    except HTTPException:
        if created:
            rclone.delete(tools.rclone, created, config)  # don't leave it behind for a retry
        raise
    return _all(st)


class EditIn(BaseModel):
    keep_days: int = Field(ge=0, le=36500)
    max_gb: int = Field(ge=0, le=10_000_000)
    enabled: bool = True
    confirm_password: str | None = Field(default=None, max_length=1024)


def _tighter(old: int, new: int) -> bool:
    """Is `new` a stricter limit than `old` (0 means no limit)?"""
    return new > 0 and (old == 0 or new < old)


@router.put("/{name}", responses={401: {}, 409: {}})
def edit(name: str, body: EditIn, request: Request, st: State) -> list[DestinationOut]:
    d = _dest_or_404(st, name)
    turning_off = d.enabled and not body.enabled
    if turning_off and d is st.settings.primary() and st.settings.copies():
        raise HTTPException(
            409, "Make another destination the primary before turning this one off."
        )
    if turning_off or _tighter(d.keep_days, body.keep_days) or _tighter(d.max_gb, body.max_gb):
        confirm_password(st, request, body.confirm_password)
    # A destination turned off keeps its `primary = true`; if the role has moved
    # since, turning it back on must not make two primaries.
    stale_primary = body.enabled and not d.enabled and st.settings.primary() is not None
    write(
        st,
        lambda doc: upsert_destination_entry(
            doc, name, keep_days=body.keep_days or None, max_gb=body.max_gb or None,
            enabled=None if body.enabled else False,
            **({"primary": None} if stale_primary else {}),
        ),
    )  # fmt: skip
    return _all(st)


class ConfirmIn(BaseModel):
    confirm_password: str | None = Field(default=None, max_length=1024)


@router.post("/{name}/primary", responses={401: {}, 409: {}})
def make_primary(name: str, body: ConfirmIn, request: Request, st: State) -> list[DestinationOut]:
    """Backups continue on this destination. It must already hold every
    snapshot the current primary holds, or footage would have no copy."""
    d = _dest_or_404(st, name)
    old = st.settings.primary()
    if not d.enabled:
        raise HTTPException(409, "Turn this destination on first.")
    if d is old:
        return _all(st)
    confirm_password(st, request, body.confirm_password)
    if old is not None:
        # What it still lacks *and wants*: its own keep_days and quota floor mean
        # it never copies the oldest snapshots, and that isn't "still copying".
        missing, _oldest = lag(st.db, st.settings, d)
        if missing:
            raise HTTPException(
                409,
                f"{missing} snapshots aren't on {name} yet (it is still copying). Make it the "
                "primary once it has caught up.",
            )

    def mutate(doc: TOMLDocument) -> None:
        if old is not None:
            upsert_destination_entry(doc, old.name, primary=None)
        upsert_destination_entry(doc, name, primary=True)

    write(st, mutate)
    return _all(st)


@router.delete("/{name}", responses={401: {}, 409: {}})
def disconnect(
    name: str, request: Request, st: State, body: ConfirmIn | None = None
) -> list[DestinationOut]:
    """Stop using a destination. Its backups are NOT deleted, and ReoVault keeps
    remembering what is on it, so connecting it again picks up where it was."""
    d = _dest_or_404(st, name)
    if d is st.settings.primary() and st.settings.copies():
        raise HTTPException(
            409, "Make another destination the primary before disconnecting this one."
        )
    confirm_password(st, request, body.confirm_password if body else None)
    write(st, lambda doc: remove_destination_entry(doc, name))
    return _all(st)


class LimitPreviewOut(BaseModel):
    snapshots: int
    bytes: int


@router.get("/{name}/preview")
def preview(name: str, keep_days: int, max_gb: int, st: State) -> LimitPreviewOut:
    """What the next retention pass would delete under these limits, from what
    the index knows (no destination is contacted)."""
    _dest_or_404(st, name)
    used = usage(st.db, name)
    gone: dict[str, retention.Snap] = {}
    for cam in {u.camera for u in used}:
        mine = [u for u in used if u.camera == cam]
        for s in retention.age_cut(mine, keep_days):
            gone[s.id] = s
    skip = set(gone)
    forget, _short, _small = retention.quota_cut(used, keep_days, max_gb, skip)
    gone.update({s.id: s for s in forget})
    return LimitPreviewOut(snapshots=len(gone), bytes=sum(s.bytes for s in gone.values()))


def check_state(st: AppState, d: DestinationConfig, alias: str) -> str:
    try:
        return restic_for(st.settings, alias, d).state()
    except ToolError as exc:
        return f"error: {st.redact(str(exc))}"
