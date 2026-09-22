"""Local-day/hour bucketing helpers, pure and framework-agnostic.

The database stores UTC; the user browses in the device's local timezone
(`devices.timezone`, IANA), and SQLite has no timezone database. Grouping by
`substr(start_utc, 1, 10)` is wrong by one or two hours' worth of clips every
single day (an evening clip lands on the wrong local day). A stored
local-day column would need a backfill for existing rows and a reindex
whenever a device's `timezone` changes, so this segment-splitting approach
is used instead.

The approach: split the requested UTC range into maximal segments where the
local UTC offset is constant (DST transitions in every IANA zone land on an
hour boundary, so hourly stepping never misses one), then let SQLite's own
`date()`/`strftime()` modifiers do the grouping per segment with the offset
bound as a parameter. `Repository` issues one query per segment and merges
by key in Python; a month has at most two segments, and the transition day
appears in both with its two partial counts simply summing.

Never apply one fixed offset across a range that might span a DST
transition: it silently misplaces a day's worth of clips twice a year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """The UTC [start, end) bounds of one local calendar day: 23h on a
    spring-forward day, 25h on a fall-back day, 24h otherwise."""
    zone = ZoneInfo(tz)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def offset_segments(
    from_utc: datetime, to_utc: datetime, tz: str
) -> list[tuple[datetime, datetime, int]]:
    """Split `[from_utc, to_utc)` into maximal segments where the local UTC
    offset is constant. Returns `(segment_start_utc, segment_end_utc,
    offset_minutes)` triples, in order."""
    if from_utc >= to_utc:
        return []
    zone = ZoneInfo(tz)
    segments: list[tuple[datetime, datetime, int]] = []
    seg_start = from_utc
    seg_offset = _offset_minutes(from_utc, zone)
    cursor = from_utc
    while cursor < to_utc:
        step_end = min(cursor + timedelta(hours=1), to_utc)
        offset = _offset_minutes(cursor, zone)
        if offset != seg_offset:
            segments.append((seg_start, cursor, seg_offset))
            seg_start = cursor
            seg_offset = offset
        cursor = step_end
    segments.append((seg_start, to_utc, seg_offset))
    return segments


def sqlite_offset_modifier(offset_minutes: int) -> str:
    """SQLite's `date()`/`strftime()` modifier syntax requires an explicit
    sign, including for zero: `'+0 minutes'` is valid, `'0 minutes'` is not."""
    return f"{offset_minutes:+d} minutes"


def _offset_minutes(instant: datetime, zone: ZoneInfo) -> int:
    delta = instant.astimezone(zone).utcoffset()
    assert delta is not None
    return int(delta.total_seconds() // 60)


if __name__ == "__main__":
    # ponytail: smallest runnable check for non-trivial branch logic (DST
    # segmentation). Full pytest coverage lives in tests/unit/test_timeline.py.
    brussels = "Europe/Brussels"

    start, end = local_day_bounds(date(2026, 6, 15), brussels)
    assert (end - start) == timedelta(hours=24), "normal day should be 24h"

    start, end = local_day_bounds(date(2026, 3, 29), brussels)  # clocks forward
    assert (end - start) == timedelta(hours=23), "spring-forward day should be 23h"

    start, end = local_day_bounds(date(2026, 10, 25), brussels)  # clocks back
    assert (end - start) == timedelta(hours=25), "fall-back day should be 25h"

    segs = offset_segments(
        datetime(2026, 3, 28, 22, 0, tzinfo=UTC), datetime(2026, 3, 29, 23, 0, tzinfo=UTC), brussels
    )
    assert len(segs) == 2, f"expected one offset change across the spring transition, got {segs}"
    assert segs[0][2] == 60 and segs[1][2] == 120, f"CET=+60min then CEST=+120min, got {segs}"

    assert sqlite_offset_modifier(0) == "+0 minutes"
    assert sqlite_offset_modifier(120) == "+120 minutes"
    assert sqlite_offset_modifier(-60) == "-60 minutes"

    print("timeline.py self-check OK")
