"""One plain verdict per camera: is its footage safe, and if not, why."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from reovault.backup import MAX_ATTEMPTS, CameraState
from reovault.db import Db, parse_iso

Verdict = Literal["up_to_date", "catching_up", "at_risk", "failing", "not_mounted", "unknown"]

# The oldest waiting clip is at risk once it's this far into the card's
# window: 0.8 leaves a fifth of the window to catch up before the loop
# overwrites it.
AT_RISK_FRACTION = 0.8


@dataclass(frozen=True, slots=True)
class CameraStatus:
    verdict: Verdict
    text: str
    pending: int
    pending_bytes: int
    eta_seconds: float | None
    oldest_pending_utc: datetime | None
    oldest_on_card_utc: datetime | None
    lost_7d: int
    failing_clips: int
    last_error: str | None


def camera_status(
    alias: str, db: Db, state: CameraState | None, now: datetime | None = None
) -> CameraStatus:
    now = now or datetime.now(UTC)
    pending, pending_bytes, oldest = db.backlog(alias)
    oldest_pending = parse_iso(oldest) if oldest else None
    oldest_on_card = state.oldest_on_card if state else None
    runs = db.runs(alias, limit=1)
    last = runs[0] if runs else None
    rate = db.throughput(alias)
    eta = pending_bytes / rate if rate and pending_bytes else None
    failing_clips = len(db.failing(alias, MAX_ATTEMPTS))
    lost = db.lost_since(alias, now - timedelta(days=7))

    verdict: Verdict
    if state and state.card and state.card.mounted is False:
        verdict, text = "not_mounted", "The camera reports no SD card."
    elif last and last.outcome == "error":
        verdict, text = "failing", f"The last backup failed: {last.error}"
    elif (
        pending
        and oldest_pending
        and oldest_on_card
        and _at_risk(oldest_pending, oldest_on_card, now)
    ):
        verdict = "at_risk"
        text = (
            f"{pending} clips are waiting and the oldest is close to being overwritten "
            "on the SD card."
        )
    elif pending:
        verdict = "catching_up"
        text = f"{pending} clips waiting" + (f", about {_human(eta)} left." if eta else ".")
    elif last is None:
        verdict, text = "unknown", "No backup has run yet."
    else:
        verdict, text = "up_to_date", "Every clip on the SD card is backed up."
    return CameraStatus(
        verdict=verdict,
        text=text,
        pending=pending,
        pending_bytes=pending_bytes,
        eta_seconds=eta,
        oldest_pending_utc=oldest_pending,
        oldest_on_card_utc=oldest_on_card,
        lost_7d=lost,
        failing_clips=failing_clips,
        last_error=last.error if last and last.outcome == "error" else None,
    )


def _at_risk(oldest_pending: datetime, oldest_on_card: datetime, now: datetime) -> bool:
    window = now - oldest_on_card
    return window > timedelta(0) and oldest_pending < now - AT_RISK_FRACTION * window


def _human(seconds: float | None) -> str:
    s = seconds or 0
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = round(s / size)
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return "under a minute"
