"""Alerts: which channels are configured (set in reovault.toml, secrets in
files), the editable rules, what's open right now, and a test send."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from reovault.alerts import Message, channel_status
from reovault.config import AlertRules
from reovault.config_store import env_locked, set_fields, table_at
from reovault.db import parse_iso
from reovault.web.api.config import write
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/alerts", tags=["alerts"])


class ChannelOut(BaseModel):
    name: str
    configured: bool


class OpenAlertOut(BaseModel):
    condition: str
    camera: str | None
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
        env_pinned=env_locked("alerts", "rules") is not None,
        channels=[ChannelOut(**c) for c in channel_status(st.settings.alerts)],
        open=[
            OpenAlertOut(
                condition=a["condition"],
                camera=a["camera"],
                title=a["title"],
                message=st.redact(a["message"]) or "",
                first_seen_at=parse_iso(a["first_seen_at"]),
                last_sent_at=parse_iso(a["last_sent_at"]) if a["last_sent_at"] else None,
            )
            for a in st.db.list_alerts()
        ],
    )


@router.get("")
def get_alerts(st: State) -> AlertsOut:
    return _alerts_out(st)


@router.put("/rules", responses={409: {}})
def put_rules(body: AlertRules, st: State) -> AlertsOut:
    if env_locked("alerts", "rules"):
        raise HTTPException(409, "Alert rules are set by environment variables.")
    write(
        st,
        lambda doc: set_fields(table_at(doc, "alerts", "rules"), body.model_dump(), AlertRules()),
    )
    st.rt.notifier.evaluate()  # apply now, not at the next minute tick
    return _alerts_out(st)


@router.post("/test", responses={409: {}})
def send_test(st: State) -> list[TestResultOut]:
    if not st.rt.notifier.channels:
        raise HTTPException(409, "No alert channel is configured.")
    results = st.rt.notifier.send(
        Message("Test alert", "If you can read this, ReoVault alerts reach you.")
    )
    return [TestResultOut(channel=r.channel, ok=r.ok, error=st.redact(r.error)) for r in results]
