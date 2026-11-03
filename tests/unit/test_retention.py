"""Which snapshots are forgotten, and which are copied: the rules that keep a
clip from ever being left with no other copy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reovault.retention import (
    CopyView,
    GiB,
    Snap,
    age_cut,
    copy_candidates,
    decide,
    quota_cut,
    release,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def snap(camera: str, days_ago: float, gb: float = 1.0, id: str | None = None) -> Snap:
    t = NOW - timedelta(days=days_ago)
    sid = id or f"{camera}-{days_ago}"
    return Snap(camera, sid, sid, t, int(gb * GiB))


def test_age_cut_counts_from_the_newest_and_never_cuts_it():
    snaps = [snap("a", 20), snap("a", 10), snap("a", 1), snap("a", 0)]
    assert [s.id for s in age_cut(snaps, 14)] == ["a-20"]
    assert age_cut(snaps, 0) == []  # 0 keeps everything


def test_an_outage_never_deletes_history():
    """The newest backup is 30 days old: everything is counted from it, not
    from today, so nothing within 14 days of it goes."""
    snaps = [snap("a", 40), snap("a", 35), snap("a", 30)]
    assert age_cut(snaps, 14) == []


def test_age_cut_is_per_camera():
    snaps = [snap("a", 30), snap("a", 0), snap("b", 30), snap("b", 25)]
    # a's newest is today, so a-30 is far older; b's newest is 25 days old, so
    # b-30 is only 5 days older than it and stays.
    assert {s.id for s in age_cut(snaps, 14)} == {"a-30"}


def test_quota_does_nothing_below_the_high_water_mark():
    usage = [snap("a", d, gb=10) for d in (5, 4, 3)]  # 30 GB of a 40 GB cap: 75%
    assert quota_cut(usage, 14, 40, set()) == ([], False, False)
    assert quota_cut(usage, 14, 0, set()) == ([], False, False)  # no cap


def test_quota_cuts_oldest_first_down_to_the_low_mark():
    usage = [snap("a", d, gb=10) for d in (6, 5, 4, 3, 2, 1, 0)]  # 70 GB of 72: 97%
    forget, short, too_small = quota_cut(usage, 14, 72, set())
    # Down to 85% of 72 = 61.2 GB: the oldest goes (60 GB left) and that's enough.
    assert [s.id for s in forget] == ["a-6"]
    usage = [snap("a", d, gb=10) for d in range(9, -1, -1)]  # 100 GB of a 100 GB cap
    forget, _, _ = quota_cut(usage, 14, 100, set())
    assert [s.id for s in forget] == ["a-9", "a-8"]  # 100 -> 90 -> 80 GB, under 85
    assert short is True  # all still inside the 14 days
    assert too_small is False


def test_quota_never_cuts_a_cameras_newest_snapshot():
    usage = [snap("a", 1, gb=50), snap("b", 0, gb=50)]
    forget, _short, too_small = quota_cut(usage, 14, 10, set())
    assert forget == []
    assert too_small is True  # a day's footage alone doesn't fit


def test_quota_is_not_short_when_the_cut_is_older_than_keep_days():
    usage = [snap("a", 40, gb=30), snap("a", 0, gb=10)]
    forget, short, _ = quota_cut(usage, 14, 40, set())
    assert [s.id for s in forget] == ["a-40"]
    assert short is False


def test_quota_skips_what_age_already_forgets():
    usage = [snap("a", 40, gb=30), snap("a", 20, gb=30), snap("a", 0, gb=10)]
    aged = {s.id for s in age_cut(usage, 14)}
    forget, _, _ = quota_cut(usage, 14, 40, aged)
    assert forget == []  # the age cut alone brings it to 10 GB


def test_a_copy_takes_newest_first_and_only_what_it_wants():
    primary = [snap("a", d) for d in (30, 10, 5, 0)]
    copy = CopyView("cloud", keep_days=14, floor=None, roots={"a": frozenset({"a-5"})})
    got = [s.id for s in copy_candidates(primary, copy)]
    assert got == ["a-0", "a-10"]  # a-5 is held, a-30 is outside its 14 days


def test_a_copy_respects_its_floor():
    primary = [snap("a", d) for d in (10, 5, 0)]
    copy = CopyView("cloud", 0, NOW - timedelta(days=6), {})
    assert [s.id for s in copy_candidates(primary, copy)] == ["a-0", "a-5"]


def test_the_primary_holds_back_what_a_copy_still_needs():
    snaps = [snap("a", 20), snap("a", 10), snap("a", 0)]
    cloud = CopyView("cloud", keep_days=0, floor=None, roots={"a": frozenset({"a-10"})})
    ok, held = release([snaps[0], snaps[1]], snaps, [cloud])
    assert [s.id for s in ok] == ["a-10"]  # a-20 isn't on the cloud yet
    assert held == 1


def test_the_primary_releases_what_a_copy_does_not_want():
    snaps = [snap("a", 30), snap("a", 0)]
    cloud = CopyView("cloud", keep_days=14, floor=None, roots={})
    ok, held = release([snaps[0]], snaps, [cloud])
    assert [s.id for s in ok] == ["a-30"]  # the cloud would forget it anyway
    assert held == 0


def test_decide_for_a_primary_with_a_quota_and_a_lagging_copy():
    """The scenario that loses footage if done naively: a small primary quota
    while the copy is still seeding. Nothing may be forgotten that isn't copied."""
    snaps = [snap("a", d, gb=10) for d in (6, 5, 4, 3, 2, 1, 0)]
    seeding = CopyView("cloud", 0, None, {"a": frozenset({"a-0", "a-1"})})  # newest first
    cut = decide(camera="a", snaps=snaps, usage=snaps, keep_days=0, max_gb=72, copies=[seeding])
    assert cut.ids == []  # the quota wants a-6, but the cloud doesn't have it yet
    assert cut.blocked == 1
    caught_up = CopyView("cloud", 0, None, {"a": frozenset(s.root for s in snaps)})
    cut = decide(camera="a", snaps=snaps, usage=snaps, keep_days=0, max_gb=72, copies=[caught_up])
    assert cut.ids == ["a-6"] and cut.blocked == 0
    assert cut.short is True  # keep_days = 0 means "forever", so any quota cut cuts it short


def test_decide_without_copies_forgets_by_age_and_quota():
    snaps = [snap("a", 40, gb=1), snap("a", 0, gb=1)]
    cut = decide(camera="a", snaps=snaps, usage=snaps, keep_days=14, max_gb=0, copies=None)
    assert cut.ids == ["a-40"]
    assert cut.blocked == 0


def test_each_cameras_cut_is_remembered_so_a_later_camera_cant_clear_it(tmp_path):
    from reovault.db import Db
    from reovault.destinations import record_cut, status_of
    from reovault.retention import Cut

    db = Db(tmp_path / "x.db")
    record_cut(db, "d", [], Cut(ids=[], floor=None, short=False, too_small=False, blocked=3), "a")
    record_cut(db, "d", [], Cut(ids=[], floor=None, short=False, too_small=False, blocked=0), "b")
    assert status_of(db, "d")["blocked"] == 3
