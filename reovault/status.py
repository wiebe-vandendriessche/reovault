"""One plain verdict per camera: is its footage safe, and if not, why."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from reovault.backup import MAX_ATTEMPTS, CameraState
from reovault.config import Settings
from reovault.db import Db, parse_iso

Verdict = Literal[
    "up_to_date", "catching_up", "at_risk", "failing", "offline", "not_mounted", "unknown"
]

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
    cam_id: str, db: Db, state: CameraState | None, now: datetime | None = None
) -> CameraStatus:
    now = now or datetime.now(UTC)
    pending, pending_bytes, oldest = db.backlog(cam_id)
    oldest_pending = parse_iso(oldest) if oldest else None
    oldest_on_card = state.oldest_on_card if state else None
    # The last *finished* run: one in progress hasn't proven anything yet
    # (on a first run the card may not even be read).
    runs = db.runs(cam_id, limit=1, finished=True)
    last = runs[0] if runs else None
    rate = db.throughput(cam_id)
    eta = pending_bytes / rate if rate and pending_bytes else None
    failing_clips = len(db.failing(cam_id, MAX_ATTEMPTS))
    lost = db.lost_since(cam_id, now - timedelta(days=7))

    verdict: Verdict
    if state and state.unreachable_since:
        since = state.unreachable_since.astimezone(UTC).strftime("%d %b %H:%M UTC")
        verdict, text = "offline", f"The camera hasn't answered since {since}."
    elif state and state.card and state.card.mounted is False:
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
        verdict, text = "unknown", "No backup has finished yet."
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


COVERAGE_BUCKETS = 96
# Until a first card scan says where the card starts.
_UNKNOWN_CARD_SPAN = timedelta(days=7)

# What a slice of time looks like in one row; when a slice holds several, the
# most urgent wins (later in this order beats earlier).
Cell = Literal["none", "safe", "waiting", "lost", "failing"]
_RANK: dict[str, int] = {"none": 0, "safe": 1, "waiting": 2, "lost": 3, "failing": 4}


@dataclass(frozen=True, slots=True)
class Run:
    start: int  # first bucket
    length: int  # buckets
    state: Cell
    clips: int  # clips in it that are in `state`


@dataclass(frozen=True, slots=True)
class CoverageRow:
    kind: Literal["card", "primary", "copy"]
    name: str
    runs: list[Run]
    marker: float | None = None  # percent along the axis: the card's oldest clip


@dataclass(frozen=True, slots=True)
class Coverage:
    start: datetime
    end: datetime
    buckets: int
    rows: list[CoverageRow]


def _runs(cells: list[tuple[Cell, int]]) -> list[Run]:
    """Adjacent buckets in the same state, merged into one run."""
    runs: list[Run] = []
    for i, (state, n) in enumerate(cells):
        if runs and runs[-1].state == state:
            last = runs[-1]
            runs[-1] = Run(last.start, last.length + 1, state, last.clips + n)
        else:
            runs.append(Run(i, 1, state, n))
    return runs


def _pick(**counts: int) -> tuple[Cell, int]:
    """The most urgent state that has clips, with how many."""
    best: tuple[Cell, int] = ("none", 0)
    for state, n in counts.items():
        if n and _RANK[state] > _RANK[best[0]]:
            best = (state, n)  # type: ignore[assignment]
    return best


def coverage_start(
    span: str, cam_id: str, db: Db, state: CameraState | None, now: datetime
) -> datetime:
    """Where a coverage axis starts: `card` (the card's oldest clip), `all` (the
    oldest clip ReoVault knows of: backed up, waiting or lost), or a number of
    days. Never less than an hour, so the axis has a width."""
    card = state.oldest_on_card if state else None
    if span == "card":
        start = card or now - _UNKNOWN_CARD_SPAN
    elif span == "all":
        start = db.oldest_kept(cam_id) or card or now - _UNKNOWN_CARD_SPAN
    else:
        start = now - timedelta(days=int(span))
    return min(start, now - timedelta(hours=1))


def coverage(
    cam_id: str,
    db: Db,
    settings: Settings,
    state: CameraState | None,
    start: datetime,
    now: datetime | None = None,
) -> Coverage:
    """Where this camera's footage from `start` to now is: on the SD card, on the
    primary, on each copy. From the index only: never calls a camera or a
    destination.

    - card: backed up (safe) or not yet (waiting) while still on the card,
      failing, and lost (overwritten first, so just before the card's start);
    - a destination: what it holds (safe) and what is still coming (waiting):
      clips on the card, and for a copy also what the primary has and the copy
      wants but hasn't copied yet. Failing and lost show on the card row only."""
    now = now or datetime.now(UTC)
    n = COVERAGE_BUCKETS
    width = (now - start) / n
    clips = db.coverage_clips(cam_id, start, now, n)  # (bucket, class) -> count
    held = db.coverage_held(cam_id, start, now, n)  # (bucket, destination) -> count

    def at(cls: str, i: int) -> int:
        return clips.get((i, cls), 0)

    card_start = state.oldest_on_card if state else None
    first = 0 if card_start is None or card_start <= start else int((card_start - start) / width)
    card: list[tuple[Cell, int]] = []
    for i in range(n):
        on_card = card_start is not None and i >= first
        card.append(
            _pick(
                failing=at("failing", i),
                lost=at("lost", i),
                waiting=at("waiting", i) if on_card else 0,
                safe=at("backed_up", i) if on_card else 0,
            )
        )
    marker = None
    if card_start is not None and start < card_start < now:
        marker = (card_start - start) / (now - start) * 100
    rows = [CoverageRow("card", "SD card", _runs(card), marker)]

    primary = settings.primary()
    for dest in [d for d in [primary, *settings.copies()] if d is not None]:
        is_primary = dest is primary
        floor_raw = None if is_primary else db.get_setting(f"copy_floor:{dest.name}")
        floor = parse_iso(floor_raw) if floor_raw else None
        cells: list[tuple[Cell, int]] = []
        for i in range(n):
            mine = held.get((i, dest.name), 0)
            coming = at("waiting", i) + at("failing", i)  # still only on the card
            if not is_primary:
                t = start + width * (i + 1)  # the slice's newest moment
                keeps = dest.keep_days == 0 or t >= now - timedelta(days=dest.keep_days)
                if keeps and (floor is None or t >= floor):
                    coming += max(0, at("backed_up", i) - mine)  # not copied yet
            cells.append(_pick(waiting=coming, safe=mine))
        rows.append(CoverageRow("primary" if is_primary else "copy", dest.name, _runs(cells)))
    return Coverage(start, now, n, rows)


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
