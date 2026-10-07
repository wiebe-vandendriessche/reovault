"""Simple-mode cron <-> form-field conversion. The day-of-week conversion is
the part most likely to be silently backwards, since cron uses Sunday=0 and
the API's `backfill_dow` uses Monday=0."""

from reovault.web.api.settings import (
    build_daily_cron,
    build_weekly_cron,
    cron_daily_time,
    cron_weekly,
)

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # the API's 0=Mon..6=Sun


def test_daily_cron_round_trips():
    cron = build_daily_cron("05:30")
    assert cron == "30 5 * * *"
    assert cron_daily_time(cron) == "05:30"


def test_default_archive_cron_parses_to_expected_time():
    assert cron_daily_time("0 5 * * *") == "05:00"


def test_weekly_cron_round_trips_for_every_day():
    for py_dow in range(7):
        cron = build_weekly_cron(py_dow, "04:15")
        got_dow, got_time = cron_weekly(cron)
        assert got_dow == py_dow, f"{_WEEKDAY_NAMES[py_dow]} round-tripped to {got_dow}"
        assert got_time == "04:15"


def test_default_backfill_cron_parses_as_sunday():
    """The shipped default `backfill_cron = '0 4 * * 0'` is cron's Sunday
    (0). As the API's Monday-first `backfill_dow` that must be 6."""
    dow, time_str = cron_weekly("0 4 * * 0")
    assert _WEEKDAY_NAMES[dow] == "Sun"
    assert time_str == "04:00"


def test_malformed_cron_falls_back_instead_of_raising():
    assert cron_daily_time("garbage") == "00:00"
    assert cron_weekly("garbage") == (6, "00:00")
