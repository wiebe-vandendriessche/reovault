"""Schedule (global default plus per-camera overrides) and retention (global)
settings, read from and written to reovault.toml (see `config_store`).

The schedule is edited in a simple mode, "daily at HH:MM" and "weekly on
<day> at HH:MM", and the server assembles/parses the 5-field cron string.
Only those two shapes are supported, exactly the two this app schedules.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from reovault import retention as retention_policy
from reovault.config import RetentionConfig, ScheduleConfig
from reovault.config_store import set_device_schedule, set_fields, table_at, upsert_device_entry
from reovault.scheduler import device_config, is_schedule_env_pinned, load_effective_schedule
from reovault.web.api.config import write
from reovault.web.deps import AppState, Device, DeviceCtx, State

router = APIRouter(tags=["settings"])

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Weekday = Annotated[int, Field(ge=0, le=6)]  # 0=Mon..6=Sun


class ScheduleBody(BaseModel):
    archive_enabled: bool
    archive_time: str
    archive_overlap_hours: int = Field(ge=1, le=24 * 30)
    backfill_enabled: bool
    backfill_dow: Weekday
    backfill_time: str
    backfill_days: int = Field(ge=1, le=3650)
    reconcile_enabled: bool
    reconcile_interval_hours: int = Field(ge=1, le=24 * 7)
    integrity_scan_enabled: bool
    integrity_scan_sample_pct: float = Field(gt=0, le=100)
    timezone: str | None = None

    @field_validator("archive_time", "backfill_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("times are HH:MM")
        return v


class ScheduleOut(ScheduleBody):
    env_pinned: bool
    # Per camera: true when it follows the global default [schedule] (no
    # [devices.schedule] of its own). Always true for the default itself.
    inherits: bool = True


class CameraScheduleBody(ScheduleBody):
    # True: drop this camera's own schedule and follow the default again;
    # the other fields are then ignored.
    inherit: bool = False


class RetentionBody(BaseModel):
    max_age_days: int | None = Field(default=None, ge=1)
    max_vault_gb: float | None = Field(default=None, ge=1)
    # Echo of `RetentionSaveOut.confirm.token` from the previous attempt.
    confirm: str | None = None


class RetentionOut(BaseModel):
    max_age_days: int | None
    max_vault_gb: float | None
    env_pinned: bool
    vault_used_bytes: int


class ConfirmOut(BaseModel):
    count: int
    bytes: int
    token: str


class RetentionSaveOut(BaseModel):
    saved: bool
    # Set when the policy would delete clips: nothing was saved, resubmit
    # with `confirm=token` to go ahead.
    confirm: ConfirmOut | None
    retention: RetentionOut


def cron_daily_time(cron: str) -> str:
    """'M H * * *' -> 'HH:MM'; '00:00' for anything that doesn't parse."""
    parts = cron.split()
    try:
        return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    except (IndexError, ValueError):
        return "00:00"


def build_daily_cron(hhmm: str) -> str:
    hour, _, minute = hhmm.partition(":")
    return f"{int(minute)} {int(hour)} * * *"


def cron_weekly(cron: str) -> tuple[int, str]:
    """'M H * * D' -> (0=Mon..6=Sun, 'HH:MM'). Cron's day-of-week is
    0=Sun..6=Sat; converted to Python's Monday-first convention."""
    parts = cron.split()
    try:
        return (int(parts[4]) - 1) % 7, f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    except (IndexError, ValueError):
        return 6, "00:00"


def build_weekly_cron(py_weekday: int, hhmm: str) -> str:
    hour, _, minute = hhmm.partition(":")
    return f"{int(minute)} {int(hour)} * * {(py_weekday + 1) % 7}"


def _schedule_out(s: ScheduleConfig, *, inherits: bool) -> ScheduleOut:
    dow, backfill_time = cron_weekly(s.backfill_cron)
    return ScheduleOut(
        archive_enabled=s.archive_enabled,
        archive_time=cron_daily_time(s.archive_cron),
        archive_overlap_hours=s.archive_overlap_hours,
        backfill_enabled=s.backfill_enabled,
        backfill_dow=dow,
        backfill_time=backfill_time,
        backfill_days=s.backfill_days,
        reconcile_enabled=s.reconcile_enabled,
        reconcile_interval_hours=s.reconcile_interval_hours,
        integrity_scan_enabled=s.integrity_scan_enabled,
        integrity_scan_sample_pct=s.integrity_scan_sample_pct,
        timezone=s.timezone,
        env_pinned=is_schedule_env_pinned(),
        inherits=inherits,
    )


def _to_schedule(body: ScheduleBody) -> ScheduleConfig:
    try:
        return ScheduleConfig(
            archive_enabled=body.archive_enabled,
            archive_cron=build_daily_cron(body.archive_time),
            archive_overlap_hours=body.archive_overlap_hours,
            backfill_enabled=body.backfill_enabled,
            backfill_cron=build_weekly_cron(body.backfill_dow, body.backfill_time),
            backfill_days=body.backfill_days,
            reconcile_enabled=body.reconcile_enabled,
            reconcile_interval_hours=body.reconcile_interval_hours,
            integrity_scan_enabled=body.integrity_scan_enabled,
            integrity_scan_sample_pct=body.integrity_scan_sample_pct,
            timezone=body.timezone or None,
        )
    except PydanticValidationError as exc:
        raise HTTPException(422, str(exc)) from None


def _camera_schedule_out(st: AppState, dev: DeviceCtx) -> ScheduleOut:
    own = device_config(st.settings, st.repo, dev.device_id)
    return _schedule_out(
        load_effective_schedule(st.settings, st.repo, device_id=dev.device_id),
        inherits=is_schedule_env_pinned() or own is None or own.schedule is None,
    )


def _require_unpinned() -> None:
    if is_schedule_env_pinned():
        raise HTTPException(409, "The schedule is set by environment variables.")


@router.get("/schedule/default")
def get_default_schedule(st: State) -> ScheduleOut:
    """The global `[schedule]` every camera follows unless it has its own."""
    return _schedule_out(st.settings.schedule, inherits=True)


@router.put("/schedule/default", responses={409: {}})
def put_default_schedule(body: ScheduleBody, st: State) -> ScheduleOut:
    _require_unpinned()
    schedule = _to_schedule(body)
    write(st, lambda doc: set_fields(table_at(doc, "schedule"), schedule.model_dump(), None))
    return _schedule_out(st.settings.schedule, inherits=True)


@router.get("/schedule")
def get_schedule(dev: Device, st: State) -> ScheduleOut:
    return _camera_schedule_out(st, dev)


@router.put("/schedule", responses={409: {}})
def put_schedule(body: CameraScheduleBody, dev: Device, st: State) -> ScheduleOut:
    """This camera's own `[devices.schedule]` in reovault.toml, or with
    `inherit`, none (back to the default). The change is applied to the
    scheduler by the config listener, like a hand edit of the file."""
    _require_unpinned()
    schedule = None if body.inherit else _to_schedule(body)
    row = st.repo.get_device(dev.device_id)
    assert row is not None  # Device dependency already resolved it

    def mutate(doc):  # type: ignore[no-untyped-def]
        upsert_device_entry(doc, row.alias, channel=row.channel, timezone=row.timezone)
        set_device_schedule(doc, row.alias, schedule)

    write(st, mutate)
    return _camera_schedule_out(st, dev)


def _retention_out(st: AppState, policy: RetentionConfig | None = None) -> RetentionOut:
    p = policy or st.settings.retention
    return RetentionOut(
        max_age_days=p.max_age_days,
        max_vault_gb=p.max_vault_gb,
        env_pinned=retention_policy.is_retention_env_pinned(),
        vault_used_bytes=st.repo.archived_ciphertext_total(),
    )


@router.get("/retention")
def get_retention(st: State) -> RetentionOut:
    return _retention_out(st)


@router.put("/retention", responses={409: {}})
def put_retention(body: RetentionBody, st: State) -> RetentionSaveOut:
    if retention_policy.is_retention_env_pinned():
        raise HTTPException(409, "Retention is set by environment variables.")
    policy = RetentionConfig(max_age_days=body.max_age_days, max_vault_gb=body.max_vault_gb)
    # Typo guard: a wrong digit must not silently wipe the archive. If the
    # new policy would delete anything, report how much and require a second
    # submit carrying a token bound to these exact values.
    token = f"{policy.max_age_days}|{policy.max_vault_gb}"
    doomed = retention_policy.preview(st.repo, policy)
    if doomed.count and body.confirm != token:
        return RetentionSaveOut(
            saved=False,
            confirm=ConfirmOut(count=doomed.count, bytes=doomed.bytes, token=token),
            retention=_retention_out(st),
        )
    write(
        st,
        lambda doc: set_fields(
            table_at(doc, "retention"),
            {"max_age_days": policy.max_age_days, "max_vault_gb": policy.max_vault_gb},
        ),
    )
    return RetentionSaveOut(saved=True, confirm=None, retention=_retention_out(st))
