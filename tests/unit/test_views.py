"""reovault/web/views.py: the Jinja view-model filters. `local_dt` is the
one with real logic (timezone conversion, string-or-datetime input,
graceful degrade on a bad tz), so it's the one that gets a real test file;
`redact_paths`/`human_bytes` are already covered where they're used."""

from __future__ import annotations

from datetime import UTC, datetime

from reovault.web.views import local_dt


def test_local_dt_formats_an_iso_string_in_the_given_timezone():
    # 21:00 UTC on 2026-09-20 is 23:00 in Europe/Brussels (CEST, UTC+2).
    result = local_dt("2026-09-20T21:00:00.000000", "Europe/Brussels")
    assert result == "20 Sep 2026, 23:00 CEST"


def test_local_dt_accepts_an_already_aware_datetime():
    """APScheduler's own `next_run_time` (see schedule_body.html's
    "Next run, per camera") is a real `datetime`, not a string."""
    dt = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    result = local_dt(dt, "Europe/Brussels")
    assert result == "20 Sep 2026, 23:00 CEST"


def test_local_dt_handles_none_and_empty_string():
    assert local_dt(None, "Europe/Brussels") == "-"
    assert local_dt("", "Europe/Brussels") == "-"


def test_local_dt_degrades_to_utc_on_an_invalid_timezone():
    """A device row with a corrupted/unknown timezone string must never
    turn a page render into a 500; UTC is always a safe fallback zone."""
    result = local_dt("2026-09-20T21:00:00.000000", "Not/A_Real_Zone")
    assert result == "20 Sep 2026, 21:00 UTC"
