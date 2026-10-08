"""Clips per day per detection type: multi-type clips count toward each
type, untagged ones as "other", days are camera-local (DST included), and
the bulk "Retry all" re-queues only this camera's problems."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from reovault.models import ErrorClass
from tests.integration.conftest import make_recording


def _add(env, name, start, rec_type, device_id=None):
    rec_id, _ = env.repository.discover_recording(
        device_id=device_id or env.device_id,
        channel=0,
        recording=make_recording(name, start, rec_type=rec_type),
    )
    return rec_id


def test_types_split_and_bucket_by_local_day(env):
    tz = "Europe/Brussels"
    # 2026-10-25 is the EU fall-back day; 23:30 UTC on the 25th is the 26th locally.
    _add(env, "a", datetime(2026, 10, 25, 10, tzinfo=UTC), "people,md")
    _add(env, "b", datetime(2026, 10, 25, 23, 30, tzinfo=UTC), "vehicle")
    _add(env, "c", datetime(2026, 10, 25, 12, tzinfo=UTC), None)
    out = env.repository.type_counts_per_day(
        device_id=env.device_id,
        from_utc=datetime(2026, 10, 24, 22, tzinfo=UTC),
        to_utc=datetime(2026, 10, 27, tzinfo=UTC),
        tz=tz,
    )
    assert out["2026-10-25"] == {"people": 1, "md": 1, "other": 1}
    assert out["2026-10-26"] == {"vehicle": 1}


def test_stats_endpoint_lists_every_local_day_and_types_busiest_first(auth_client, env):
    # Just before now: the window ends at the current instant.
    today = datetime.now(ZoneInfo("Europe/Brussels")) - timedelta(seconds=10)
    for i, t in enumerate(["people", "people", "md"]):
        _add(env, f"x{i}", (today - timedelta(seconds=i)).astimezone(UTC), t)
    body = auth_client.get("/api/v1/stats/types", params={"days": 7}).json()
    assert body["types"] == ["people", "md"]
    assert len(body["days"]) == 7
    assert body["days"][-1] == {"day": today.date().isoformat(), "counts": {"people": 2, "md": 1}}
    assert body["days"][0]["counts"] == {}


def test_retry_all_requeues_only_this_cameras_problems(auth_client, env):
    from reovault.providers.fake import FakeProvider

    _other, other_id = env.archiver_for("garage", FakeProvider())
    mine = [_add(env, f"f{i}", datetime(2026, 4, 17, 10, i, tzinfo=UTC), "md") for i in range(3)]
    theirs = _add(env, "g", datetime(2026, 4, 17, 11, tzinfo=UTC), "md", device_id=other_id)
    for rid in [*mine, theirs]:
        env.repository.mark_failed(
            rid, error="x", error_class=ErrorClass.NETWORK, next_attempt_at=None
        )
    env.repository.mark_quarantined(mine[2], error="bad", error_class=ErrorClass.PROTOCOL)

    response = auth_client.post("/api/v1/problems/retry", params={"device": env.device_id})
    assert response.json() == {"count": 3}
    for rid in mine:
        row = env.repository.get(rid)
        assert row.state == "failed" and row.attempts == 0 and row.next_attempt_at
    assert env.repository.get(theirs).next_attempt_at is None  # other camera untouched


def test_retry_all_needs_csrf(auth_client):
    response = auth_client.post("/api/v1/problems/retry", headers={"X-CSRF-Token": ""})
    assert response.status_code == 403
