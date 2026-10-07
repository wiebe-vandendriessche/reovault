"""Devices: the fleet list, enabling/disabling a camera, LAN discovery, and
adding a camera. Fleet-wide, so nothing here is device-scoped."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

from reovault.archiver import Archiver
from reovault.db.repository import DeviceRow, StorageSampleRow, parse_iso_utc
from reovault.providers.base import ProviderError
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.scheduler import add_device_jobs, load_effective_schedule, remove_device_jobs
from reovault.web.api.common import RunOut, run_out
from reovault.web.api.health import SdBarOut, StorageSampleOut, on_card, sample_out, sd_bar
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceSummaryOut(BaseModel):
    """The navbar device switcher's row: cheap, no camera calls."""

    id: int
    alias: str
    name: str | None
    timezone: str
    problems: int


class DeviceOut(BaseModel):
    id: int
    alias: str
    host: str | None
    model: str | None
    name: str | None
    timezone: str
    enabled: bool
    archived_count: int
    archived_bytes: int
    sample: StorageSampleOut | None
    sd: SdBarOut | None
    last_run: RunOut | None
    gateway_down: bool | None  # None: not a reolink-cli device, or disabled


class DevicesOut(BaseModel):
    devices: list[DeviceOut]
    registry_available: bool


class DiscoveredOut(BaseModel):
    host: str
    uid: str | None
    mac: str | None
    protocol: str | None
    model: str | None
    name: str | None
    already_registered: bool


class AddDeviceIn(BaseModel):
    """Password is `SecretStr` so an accidental `repr()` anywhere, including
    a structlog kwarg, renders `**********` instead of the plaintext."""

    alias: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=1024)
    channel: int | None = Field(default=None, ge=0, le=255)
    timezone: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=100)


class EnabledIn(BaseModel):
    enabled: bool


def _backfill_identity(st: AppState, d: DeviceRow) -> tuple[str | None, str | None, str | None]:
    """Fills `host`/`model`/`name` from reolink-cli when TOML seeding left
    them unset, caching the result so this runs once per device. `host`
    comes from a local registry call; `model`/`name` need the gateway, so a
    sleeping camera or a down gateway degrades to placeholders instead of
    blocking the page."""
    host, model, name = d.host, d.model, d.name
    if st.registry is None or None not in (host, model, name):
        return host, model, name
    if host is None:
        with contextlib.suppress(ProviderError):
            host = next(
                (rd.host for rd in st.registry.list_devices() if rd.alias == d.alias and rd.host),
                None,
            )
    if model is None or name is None:
        try:
            info = st.registry.device_info(alias=d.alias)
            model, name = info.model, info.name
        except ProviderError:
            pass
    if (host, model, name) != (d.host, d.model, d.name):
        st.repo.upsert_device(
            alias=d.alias, channel=d.channel, timezone=d.timezone, model=model, host=host, name=name
        )
    return host, model, name


def _refresh_sample(
    st: AppState, d: DeviceRow, archiver: Archiver | None
) -> StorageSampleRow | None:
    """A cheap on-demand SD read (`storage status` only, never the 3650-day
    VOD scan the coverage alarm uses). Skipped when a sample under an hour
    old exists, or the gateway is down, to keep the page fast."""
    sample = st.repo.latest_storage_sample(d.id)
    if archiver is None or not isinstance(archiver.provider, ReolinkCliProvider):
        return sample
    if not archiver.provider.gateway.is_listening():
        return sample
    if sample is not None and datetime.now(UTC) - parse_iso_utc(sample.sampled_at) < timedelta(
        hours=1
    ):
        return sample
    try:
        storage = archiver.provider.storage_status()
    except ProviderError:
        return sample
    st.repo.record_storage_sample(
        device_id=d.id,
        total_gb=storage.total_gb,
        remain_gb=storage.remain_gb,
        formatted=storage.formatted,
        mounted=storage.mounted,
        oldest_recording_utc=(
            parse_iso_utc(sample.oldest_recording_utc)
            if sample is not None and sample.oldest_recording_utc
            else None
        ),
    )
    return st.repo.latest_storage_sample(d.id)


def _devices_out(st: AppState) -> DevicesOut:
    out = []
    for d in st.repo.list_devices():
        host, model, name = _backfill_identity(st, d)
        totals = st.repo.totals(d.id)
        archiver = st.fleet.get(d.id)
        down = None
        if archiver is not None and isinstance(archiver.provider, ReolinkCliProvider):
            down = not archiver.provider.gateway.is_listening()
        sample = _refresh_sample(st, d, archiver)
        last_run = st.repo.last_finished_run(d.id)
        out.append(
            DeviceOut(
                id=d.id,
                alias=d.alias,
                host=host,
                model=model,
                name=name,
                timezone=d.timezone,
                enabled=d.enabled,
                archived_count=totals.archived_count,
                archived_bytes=totals.archived_plaintext_bytes,
                sample=sample_out(sample),
                sd=sd_bar(sample, on_card(st, d.id, totals.first_start_utc)),
                last_run=run_out(st, last_run) if last_run else None,
                gateway_down=down,
            )
        )
    return DevicesOut(devices=out, registry_available=st.registry is not None)


@router.get("")
def list_devices(st: State) -> DevicesOut:
    return _devices_out(st)


@router.get("/enabled")
def list_enabled(st: State) -> list[DeviceSummaryOut]:
    ids = set(st.fleet.ids())
    out = []
    for d in st.repo.list_devices():
        if d.id not in ids:
            continue
        totals = st.repo.totals(d.id)
        out.append(
            DeviceSummaryOut(
                id=d.id,
                alias=d.alias,
                name=d.name,
                timezone=d.timezone,
                problems=totals.failed_count + totals.quarantined_count,
            )
        )
    return out


@router.post("/discover", responses={502: {}, 503: {}})
def discover(st: State) -> list[DiscoveredOut]:
    if st.registry is None:
        raise HTTPException(503, "Camera discovery isn't available on this server.")
    try:
        found = st.registry.discover()
    except ProviderError as exc:
        raise HTTPException(502, st.redact(str(exc))) from None
    return [
        DiscoveredOut(
            host=r.host,
            uid=r.uid,
            mac=r.mac,
            protocol=r.protocol,
            model=r.model,
            name=r.name,
            already_registered=r.already_registered,
        )
        for r in found
    ]


def _enable_in_fleet(st: AppState, device_id: int) -> None:
    row = st.repo.get_device(device_id)
    if row is None:
        return
    archiver = st.fleet.add(row)
    if st.scheduler is not None:
        schedule = load_effective_schedule(st.settings, st.repo, device_id=device_id)
        add_device_jobs(st.scheduler, archiver, schedule, device_timezone=row.timezone)


@router.post("", status_code=201, responses={409: {}, 422: {}, 502: {}, 503: {}})
def add_device(body: AddDeviceIn, st: State) -> DevicesOut:
    if st.registry is None:
        raise HTTPException(503, "Adding a camera isn't available on this server.")
    try:
        ZoneInfo(body.timezone)
    except Exception:  # noqa: BLE001 - any zoneinfo failure is the same user-facing error
        raise HTTPException(422, f"Unknown timezone: {body.timezone!r}") from None
    # An alias can already exist in either namespace: ReoVault's devices
    # table or reolink-cli's own registry.
    existing = {d.alias for d in st.repo.list_devices()} | {
        d.alias for d in st.registry.list_devices()
    }
    if body.alias in existing:
        raise HTTPException(409, f"{body.alias!r} is already registered.")
    try:
        st.registry.add_device(
            alias=body.alias,
            host=body.host,
            user=body.user,
            password=body.password,
            channel=body.channel,
        )
    except ProviderError as exc:
        raise HTTPException(502, f"Could not add the camera: {st.redact(str(exc))}") from None
    device_id = st.repo.upsert_device(
        alias=body.alias,
        channel=body.channel or 0,
        timezone=body.timezone,
        host=body.host,
        user=body.user,
        name=body.name,
    )
    _enable_in_fleet(st, device_id)
    st.bus.publish("devices")
    return _devices_out(st)


@router.put("/{device_id}/enabled")
def set_enabled(device_id: int, body: EnabledIn, st: State) -> DevicesOut:
    row = st.repo.get_device(device_id)
    if row is None:
        raise HTTPException(404, "Unknown camera.")
    if body.enabled != row.enabled:
        st.repo.set_device_enabled(device_id, body.enabled)
        if body.enabled:
            _enable_in_fleet(st, device_id)
        else:
            st.fleet.remove(device_id)
            if st.scheduler is not None:
                remove_device_jobs(st.scheduler, device_id)
        st.bus.publish("devices")
    return _devices_out(st)
