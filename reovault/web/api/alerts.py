"""Alerts: which channels are configured (read-only here, they're set in
compose/TOML with secrets in files), the editable rules, what's open right
now, and a test send."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from reovault.config import AlertRules
from reovault.config_store import set_fields, table_at
from reovault.db.repository import parse_iso_utc
from reovault.notify import (
    Message,
    channel_status,
    is_rules_env_pinned,
)
from reovault.web.api.config import write
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/alerts", tags=["alerts"])


class ChannelOut(BaseModel):
    name: str
    configured: bool


class OpenAlertOut(BaseModel):
    condition: str
    device_id: int | None
    title: str
    message: str
    first_seen_at: datetime
    last_sent_at: datetime | None


class AlertsOut(BaseModel):
    rules: AlertRules
    env_pinned: bool
    channels: list[ChannelOut]
    open: list[OpenAlertOut]


class TestResultOut(BaseModel):
    channel: str
    ok: bool
    error: str | None


def _alerts_out(st: AppState) -> AlertsOut:
    return AlertsOut(
        rules=st.settings.alerts.rules,
        env_pinned=is_rules_env_pinned(),
        channels=[ChannelOut(**c) for c in channel_status(st.settings.alerts)],
        open=[
            OpenAlertOut(
                condition=a.condition,
                device_id=a.device_id,
                title=a.title,
                message=a.message,
                first_seen_at=parse_iso_utc(a.first_seen_at),
                last_sent_at=parse_iso_utc(a.last_sent_at) if a.last_sent_at else None,
            )
            for a in st.repo.list_alerts()
        ],
    )


@router.get("")
def get_alerts(st: State) -> AlertsOut:
    return _alerts_out(st)


@router.put("/rules", responses={409: {}})
def put_rules(body: AlertRules, st: State) -> AlertsOut:
    if is_rules_env_pinned():
        raise HTTPException(409, "Alert rules are set by environment variables.")
    write(
        st,
        lambda doc: set_fields(table_at(doc, "alerts", "rules"), body.model_dump(), AlertRules()),
    )
    st.notifier.evaluate()  # apply now, not at the next minute tick
    return _alerts_out(st)


@router.post("/test", responses={409: {}})
def send_test(st: State) -> list[TestResultOut]:
    if not st.notifier.channels:
        raise HTTPException(409, "No alert channel is configured.")
    results = st.notifier.send(
        Message("Test alert", "If you can read this, ReoVault alerts reach you.")
    )
    return [TestResultOut(channel=r.channel, ok=r.ok, error=st.redact(r.error)) for r in results]
