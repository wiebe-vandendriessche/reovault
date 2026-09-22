"""Query performance at scale: ~300k rows (roughly one year at 858/day). Not
run by default (see the `slow` marker) since it takes real wall-clock time
to seed; run explicitly with `pytest -m slow`.
"""

import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from reovault.db.repository import Repository

pytestmark = pytest.mark.slow

ROWS_PER_DAY = 858
DAYS = 365
TOTAL_ROWS = ROWS_PER_DAY * DAYS


@pytest.fixture(scope="module")
def big_repo(tmp_path_factory):
    """Bulk-inserted directly via SQL, bypassing `discover_recording`'s
    per-row transaction: this fixture is about query performance, not
    re-testing the dedup/state-machine logic already covered elsewhere."""
    tmp_path = tmp_path_factory.mktemp("scale")
    repo = Repository(tmp_path / "reovault.db")
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")

    rng = random.Random(42)
    rec_types = ["md", "md,people", "md,vehicle", "md,people,vehicle", "sched"]
    start = datetime(2025, 9, 19, tzinfo=UTC)

    rows = []
    for day in range(DAYS):
        day_start = start + timedelta(days=day)
        for i in range(ROWS_PER_DAY):
            ts = day_start + timedelta(seconds=int(i * 86400 / ROWS_PER_DAY))
            iso = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
            size = rng.randint(10_000_000, 30_000_000)
            state = "archived" if rng.random() > 0.0001 else "failed"
            rows.append(
                (
                    device_id,
                    0,
                    f"{ts:%Y%m%d%H%M%S}",
                    iso,
                    iso,
                    30.0,
                    rng.choice(rec_types),
                    "main",
                    size,
                    state,
                    f"doorbell/{ts:%Y/%m/%d}/{ts:%Y%m%dT%H%M%SZ}_clip.enc"
                    if state == "archived"
                    else None,
                    "a" * 64 if state == "archived" else None,
                    size if state == "archived" else None,
                    size + 100 if state == "archived" else None,
                    0,
                    None,
                    None,
                    None,
                    iso,
                    iso if state == "archived" else None,
                    "{}",
                )
            )

    with repo.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO recordings (
                device_id, channel, remote_name, start_utc, end_utc, duration_s,
                rec_type, stream, remote_size, state, vault_path, plaintext_sha256,
                plaintext_size, ciphertext_size, attempts, last_error, last_error_class,
                next_attempt_at, first_seen_at, archived_at, raw_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    repo.conn.execute("PRAGMA optimize")
    yield repo, device_id
    repo.close()


def test_seeded_row_count(big_repo):
    repo, device_id = big_repo
    totals = repo.totals(device_id)
    assert totals.archived_count + totals.failed_count == TOTAL_ROWS


def test_day_view_stays_fast(big_repo):
    repo, device_id = big_repo
    from reovault.timeline import local_day_bounds

    day_start, day_end = local_day_bounds(
        datetime(2026, 3, 15, tzinfo=UTC).date(), "Europe/Brussels"
    )
    t0 = time.perf_counter()
    rows = repo.hour_buckets(
        device_id=device_id, from_utc=day_start, to_utc=day_end, tz="Europe/Brussels"
    )
    elapsed = time.perf_counter() - t0
    assert sum(r.total for r in rows) > 0
    assert elapsed < 0.5, f"day view took {elapsed * 1000:.1f}ms, expected well under 500ms"


def test_calendar_month_stays_fast(big_repo):
    """Mirrors what app.py's calendar fragment actually does: bounds come
    from `local_day_bounds` on the first/last day of the month, not raw
    UTC month boundaries (which don't align with local days and would
    silently pull in a sliver of the adjacent month)."""
    from reovault.timeline import local_day_bounds

    repo, device_id = big_repo
    from_utc, _ = local_day_bounds(datetime(2026, 3, 1, tzinfo=UTC).date(), "Europe/Brussels")
    _, to_utc = local_day_bounds(datetime(2026, 3, 31, tzinfo=UTC).date(), "Europe/Brussels")

    t0 = time.perf_counter()
    buckets = repo.day_buckets(
        device_id=device_id, from_utc=from_utc, to_utc=to_utc, tz="Europe/Brussels"
    )
    elapsed = time.perf_counter() - t0
    assert len(buckets) <= 31
    assert elapsed < 0.5, f"calendar month took {elapsed * 1000:.1f}ms, expected well under 500ms"


def test_problems_list_stays_fast_when_healthy(big_repo):
    """A healthy system (near-zero matching rows) is the *slowest* case
    without the partial index -- the pathological case to guard against."""
    repo, device_id = big_repo
    t0 = time.perf_counter()
    rows = repo.problems(device_id=device_id, limit=100)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"problems list took {elapsed * 1000:.1f}ms, expected well under 100ms"
    assert isinstance(rows, list)


def test_totals_query_uses_covering_index(big_repo):
    repo, _device_id = big_repo
    plan = repo._fetchall(
        """
        EXPLAIN QUERY PLAN
        SELECT SUM(plaintext_size) FROM recordings WHERE device_id = 1 AND state = 'archived'
        """
    )
    plan_text = " ".join(row["detail"] for row in plan)
    assert "idx_rec_archived_size" in plan_text or "COVERING INDEX" in plan_text.upper(), plan_text


def test_totals_stays_fast(big_repo):
    repo, device_id = big_repo
    t0 = time.perf_counter()
    totals = repo.totals(device_id)
    elapsed = time.perf_counter() - t0
    assert totals.archived_count > 0
    assert elapsed < 1.0, f"totals took {elapsed * 1000:.1f}ms"
