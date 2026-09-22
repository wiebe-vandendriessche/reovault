"""Repository dashboard queries, DST bucketing, and keyset pagination."""

from datetime import UTC, datetime, timedelta

import pytest

from reovault.db.repository import Repository
from tests.unit.conftest import make_recording


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "reovault.db")
    yield r
    r.close()


def _rec(name, start, *, rec_type="md", size=1024):
    return make_recording(name, start, duration_s=30.0, rec_type=rec_type, size=size)


def _archive(repo, device_id, name, start, *, rec_type="md", size=1024, state="archived"):
    rec = _rec(name, start, rec_type=rec_type, size=size)
    rid, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    if state == "archived":
        repo.transition_downloading(rid)
        repo.transition_verifying(rid)
        repo.finalize_archived(
            rid,
            vault_path=f"{name}.enc",
            plaintext_sha256="a" * 64,
            plaintext_size=size,
            ciphertext_size=size + 100,
        )
    elif state == "failed":
        repo.transition_downloading(rid)
        from reovault.models import ErrorClass

        repo.mark_failed(rid, error="boom", error_class=ErrorClass.NETWORK, next_attempt_at=None)
    elif state == "quarantined":
        repo.transition_downloading(rid)
        from reovault.models import ErrorClass

        repo.mark_quarantined(rid, error="bad", error_class=ErrorClass.INPUT)
    return rid


# -- ping / get_device --------------------------------------------------


def test_ping_does_not_raise(repo):
    repo.ping()


def test_get_device_roundtrip(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    device = repo.get_device(device_id)
    assert device is not None
    assert device.alias == "doorbell"
    assert device.timezone == "Europe/Brussels"
    assert repo.get_device(999999) is None


# -- local-day bucketing / DST -------------------------------------------


def test_day_buckets_europe_brussels_day_boundary(repo):
    """A 23:30 UTC clip belongs to the *next* local day in summer (CEST,
    UTC+2); a 22:30 UTC clip on the same date belongs to the current one."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    _archive(
        repo, device_id, "a", datetime(2026, 6, 15, 22, 30, tzinfo=UTC)
    )  # -> local Jun 16 00:30
    _archive(
        repo, device_id, "b", datetime(2026, 6, 15, 21, 30, tzinfo=UTC)
    )  # -> local Jun 15 23:30

    buckets = {
        b.day: b
        for b in repo.day_buckets(
            device_id=device_id,
            from_utc=datetime(2026, 6, 15, tzinfo=UTC),
            to_utc=datetime(2026, 6, 17, tzinfo=UTC),
            tz="Europe/Brussels",
        )
    }
    assert buckets["2026-06-15"].total == 1
    assert buckets["2026-06-16"].total == 1


def test_day_buckets_across_dst_spring_forward_sums_correctly(repo):
    """2026-03-29 is a 23h day in Europe/Brussels. Clips on both sides of
    the transition must land in exactly one day bucket each, with nothing
    double-counted or dropped."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    # Before transition (CET, UTC+1): 2026-03-28 23:30 UTC -> local 2026-03-29 00:30
    _archive(repo, device_id, "a", datetime(2026, 3, 28, 23, 30, tzinfo=UTC))
    # After transition (CEST, UTC+2): 2026-03-29 21:30 UTC -> local 2026-03-29 23:30
    _archive(repo, device_id, "b", datetime(2026, 3, 29, 21, 30, tzinfo=UTC))
    # Clearly the next day
    _archive(repo, device_id, "c", datetime(2026, 3, 30, 10, 0, tzinfo=UTC))

    buckets = {
        b.day: b
        for b in repo.day_buckets(
            device_id=device_id,
            from_utc=datetime(2026, 3, 28, tzinfo=UTC),
            to_utc=datetime(2026, 3, 31, tzinfo=UTC),
            tz="Europe/Brussels",
        )
    }
    assert buckets["2026-03-29"].total == 2
    assert buckets["2026-03-30"].total == 1
    assert sum(b.total for b in buckets.values()) == 3


def test_hour_buckets_fold_the_repeated_hour_on_fall_back_day(repo):
    """2026-10-25 is a 25h day: 02:00-03:00 local happens twice. Both
    occurrences must fold into a single hour=2 bucket, not silently drop
    one or split across two hour keys."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    # First occurrence of local 02:30 (CEST, UTC+2): 00:30 UTC
    _archive(repo, device_id, "a", datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    # Second occurrence of local 02:30 (CET, UTC+1): 01:30 UTC
    _archive(repo, device_id, "b", datetime(2026, 10, 25, 1, 30, tzinfo=UTC))

    from datetime import date

    from reovault.timeline import local_day_bounds

    start, end = local_day_bounds(date(2026, 10, 25), "Europe/Brussels")
    assert (end - start) == timedelta(hours=25)

    buckets = {
        b.hour: b
        for b in repo.hour_buckets(
            device_id=device_id, from_utc=start, to_utc=end, tz="Europe/Brussels"
        )
    }
    assert buckets[2].total == 2, (
        "both instances of the repeated local hour must fold into one bucket"
    )
    assert sum(b.total for b in buckets.values()) == 2


def test_day_buckets_over_a_month_matches_independent_counts(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    for day in range(1, 29):
        _archive(repo, device_id, f"d{day}", datetime(2026, 2, day, 12, 0, tzinfo=UTC))

    buckets = repo.day_buckets(
        device_id=device_id,
        from_utc=datetime(2026, 2, 1, tzinfo=UTC),
        to_utc=datetime(2026, 3, 1, tzinfo=UTC),
        tz="Europe/Brussels",
    )
    assert len(buckets) <= 31
    assert sum(b.total for b in buckets) == 28


# -- recordings_in_window / rec_type filter ------------------------------


def test_recordings_in_window_filters_multivalued_rec_type(repo):
    """rec_type is a comma-separated list on the real camera; `people` must
    match `md,people,vehicle` but not `md,vehicle` or `peoples`."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    _archive(
        repo, device_id, "a", datetime(2026, 1, 1, 10, 0, tzinfo=UTC), rec_type="md,people,vehicle"
    )
    _archive(repo, device_id, "b", datetime(2026, 1, 1, 11, 0, tzinfo=UTC), rec_type="md,vehicle")
    _archive(repo, device_id, "c", datetime(2026, 1, 1, 12, 0, tzinfo=UTC), rec_type="peoples")

    rows = repo.recordings_in_window(
        device_id=device_id,
        from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        to_utc=datetime(2026, 1, 2, tzinfo=UTC),
        rec_type="people",
    )
    assert [r.remote_name for r in rows] == ["a"]


def test_recordings_in_window_keyset_pagination_is_complete_and_non_overlapping(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    for i in range(10):
        _archive(repo, device_id, f"r{i}", datetime(2026, 1, 1, i, 0, tzinfo=UTC))

    page1 = repo.recordings_in_window(
        device_id=device_id,
        from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        to_utc=datetime(2026, 1, 2, tzinfo=UTC),
        limit=4,
    )
    assert len(page1) == 4
    cursor = (page1[-1].start_utc, page1[-1].id)
    page2 = repo.recordings_in_window(
        device_id=device_id,
        from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        to_utc=datetime(2026, 1, 2, tzinfo=UTC),
        limit=100,
        after=cursor,
    )
    all_names = [r.remote_name for r in page1] + [r.remote_name for r in page2]
    assert len(all_names) == 10
    assert len(set(all_names)) == 10  # no overlap, nothing dropped


# -- problems -------------------------------------------------------------


def test_problems_returns_failed_and_quarantined_only(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    _archive(repo, device_id, "ok", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), state="archived")
    _archive(repo, device_id, "f", datetime(2026, 1, 1, 2, 0, tzinfo=UTC), state="failed")
    _archive(repo, device_id, "q", datetime(2026, 1, 1, 3, 0, tzinfo=UTC), state="quarantined")

    rows = repo.problems(device_id=device_id)
    names = {r.remote_name for r in rows}
    assert names == {"f", "q"}


def test_problems_keyset_pagination_descending(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    for i in range(5):
        _archive(repo, device_id, f"p{i}", datetime(2026, 1, 1, i, 0, tzinfo=UTC), state="failed")

    page1 = repo.problems(device_id=device_id, limit=2)
    assert [r.remote_name for r in page1] == ["p4", "p3"]
    cursor = (page1[-1].start_utc, page1[-1].id)
    page2 = repo.problems(device_id=device_id, limit=100, after=cursor)
    assert [r.remote_name for r in page2] == ["p2", "p1", "p0"]


# -- totals -----------------------------------------------------------------


def test_totals_matches_naive_aggregation(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    _archive(
        repo, device_id, "a", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), size=1000, state="archived"
    )
    _archive(
        repo, device_id, "b", datetime(2026, 1, 1, 2, 0, tzinfo=UTC), size=2000, state="archived"
    )
    _archive(repo, device_id, "c", datetime(2026, 1, 1, 3, 0, tzinfo=UTC), state="failed")
    _archive(repo, device_id, "d", datetime(2026, 1, 1, 4, 0, tzinfo=UTC), state="quarantined")

    totals = repo.totals(device_id)
    assert totals.archived_count == 2
    assert totals.archived_plaintext_bytes == 3000
    assert totals.failed_count == 1
    assert totals.quarantined_count == 1
    assert totals.first_start_utc is not None
    assert totals.last_archived_at is not None


# -- on_card_bytes (device card's stacked SD usage bar) --------------------


def test_on_card_bytes_with_no_sample_returns_zero_without_scanning(repo):
    """`since_utc=None` (no storage sample recorded yet) must return zeroes
    rather than summing every recording ever seen for this device, which
    would misrepresent bytes long since overwritten on the card as still
    being "on the card"."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    _archive(repo, device_id, "a", datetime(2026, 1, 1, tzinfo=UTC), size=5000, state="archived")

    on_card = repo.on_card_bytes(device_id, None)
    assert on_card.archived_bytes == 0
    assert on_card.pending_bytes == 0


def test_on_card_bytes_splits_archived_from_pending_within_the_window(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    since = datetime(2026, 1, 2, tzinfo=UTC)
    # Before the card's retention window: must never be counted, archived or not.
    _archive(repo, device_id, "old", datetime(2026, 1, 1, tzinfo=UTC), size=9999, state="archived")
    _archive(
        repo, device_id, "a", datetime(2026, 1, 2, 1, 0, tzinfo=UTC), size=1000, state="archived"
    )
    _archive(
        repo, device_id, "b", datetime(2026, 1, 2, 2, 0, tzinfo=UTC), size=2000, state="archived"
    )
    _archive(repo, device_id, "c", datetime(2026, 1, 2, 3, 0, tzinfo=UTC), size=500, state="failed")
    _archive(
        repo, device_id, "d", datetime(2026, 1, 2, 4, 0, tzinfo=UTC), size=700, state="quarantined"
    )

    on_card = repo.on_card_bytes(device_id, since)
    assert on_card.archived_bytes == 3000
    assert on_card.pending_bytes == 1200


def test_on_card_bytes_ignores_a_null_remote_size(repo):
    """A recording predating `remote_size` being captured (or one the
    camera itself never reported a size for) must degrade to 0, not raise
    or poison the whole SUM to NULL. `remote_size` (what the camera
    reported) is null here while `plaintext_size` (what finalize_archived
    stored) is not, since those are two different fields on purpose."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    since = datetime(2026, 1, 1, tzinfo=UTC)
    _archive(
        repo, device_id, "a", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), size=1000, state="archived"
    )
    rec = _rec("b", datetime(2026, 1, 1, 2, 0, tzinfo=UTC), size=None)
    rid, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    repo.transition_downloading(rid)
    repo.transition_verifying(rid)
    repo.finalize_archived(
        rid,
        vault_path="b.enc",
        plaintext_sha256="b" * 64,
        plaintext_size=1000,
        ciphertext_size=1100,
    )

    on_card = repo.on_card_bytes(device_id, since)
    assert on_card.archived_bytes == 1000


# -- runs ---------------------------------------------------------------


def test_running_run_and_last_finished_run(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    run_id = repo.start_run(
        device_id=device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert repo.running_run(device_id) is not None
    assert repo.running_run(device_id).id == run_id
    assert repo.last_finished_run(device_id) is None

    repo.finish_run(run_id, outcome="success", downloaded=3, bytes_archived=3000)
    assert repo.running_run(device_id) is None
    finished = repo.last_finished_run(device_id)
    assert finished is not None
    assert finished.outcome == "success"


def test_abort_stale_running_runs_closes_only_that_devices_open_row(repo):
    device_a = repo.upsert_device(alias="a", channel=0, timezone="UTC")
    device_b = repo.upsert_device(alias="b", channel=0, timezone="UTC")
    run_a = repo.start_run(
        device_id=device_a,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    run_b = repo.start_run(
        device_id=device_b,
        trigger="scheduled",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )

    closed = repo.abort_stale_running_runs(device_a, error="interrupted by a process restart")

    assert closed == 1
    assert repo.running_run(device_a) is None
    run_a_row = repo.get_run(run_a)
    assert run_a_row.outcome == "aborted"
    assert run_a_row.error == "interrupted by a process restart"
    # Device b's own in-flight run must be untouched.
    assert repo.running_run(device_b) is not None
    assert repo.running_run(device_b).id == run_b


def test_abort_stale_running_runs_is_a_no_op_with_nothing_running(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    assert repo.abort_stale_running_runs(device_id, error="x") == 0


def test_recent_runs_keyset_pagination(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    run_ids = []
    for _i in range(5):
        rid = repo.start_run(
            device_id=device_id,
            trigger="manual",
            window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
            window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
        )
        repo.finish_run(rid, outcome="success")
        run_ids.append(rid)

    page1 = repo.recent_runs(device_id=device_id, limit=2)
    assert [r.id for r in page1] == run_ids[::-1][:2]
    page2 = repo.recent_runs(device_id=device_id, limit=100, before_id=page1[-1].id)
    assert [r.id for r in page2] == run_ids[::-1][2:]


def test_runs_per_day_attributes_bytes_to_local_start_date(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    run_id = repo.start_run(
        device_id=device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 6, 15, tzinfo=UTC),
        window_to_utc=datetime(2026, 6, 16, tzinfo=UTC),
    )
    # started_at is set to "now" by start_run; patch it directly to a known
    # UTC instant that's a different local day (23:30 UTC = next day CEST).
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE archive_runs SET started_at = ? WHERE id = ?",
            ("2026-06-15T23:30:00.000000", run_id),
        )
    repo.finish_run(run_id, outcome="success", bytes_archived=5000, downloaded=2)

    rows = repo.runs_per_day(
        device_id=device_id,
        from_utc=datetime(2026, 6, 15, tzinfo=UTC),
        to_utc=datetime(2026, 6, 17, tzinfo=UTC),
        tz="Europe/Brussels",
    )
    by_day = {r.day: r for r in rows}
    assert by_day["2026-06-16"].bytes_archived == 5000  # 23:30 UTC = 01:30 local next day


# -- iter_archived --------------------------------------------------------


def test_iter_archived_yields_every_row_exactly_once_across_batches(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    for i in range(23):
        _archive(repo, device_id, f"a{i}", datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i))

    names = [r.remote_name for r in repo.iter_archived(device_id=device_id, batch_size=7)]
    assert len(names) == 23
    assert len(set(names)) == 23


def test_iter_archived_concurrent_from_two_threads_does_not_deadlock(repo):
    import threading

    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    for i in range(20):
        _archive(repo, device_id, f"c{i}", datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i))

    results: list[int] = []
    errors: list[Exception] = []

    def worker():
        try:
            rows = list(repo.iter_archived(device_id=device_id, batch_size=3))
            results.append(len(rows))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert results == [20, 20]


# -- retry_recording --------------------------------------------------------


def test_retry_recording_moves_failed_and_quarantined_to_failed(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    fid = _archive(repo, device_id, "f", datetime(2026, 1, 1, tzinfo=UTC), state="failed")
    qid = _archive(
        repo, device_id, "q", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), state="quarantined"
    )

    assert repo.retry_recording(fid) is True
    assert repo.retry_recording(qid) is True

    row_f = repo.get(fid)
    assert row_f.state == "failed"
    assert row_f.attempts == 0


def test_retry_recording_returns_false_for_archived_row(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    aid = _archive(repo, device_id, "a", datetime(2026, 1, 1, tzinfo=UTC), state="archived")
    assert repo.retry_recording(aid) is False


def test_retry_recording_returns_false_for_missing_row(repo):
    assert repo.retry_recording(999999) is False
