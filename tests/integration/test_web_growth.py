"""Footage growth: one camera's archived bytes over the last N camera-local
days, today included, bucketed by the day each clip was *recorded*."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.conftest import make_recording


def test_growth_buckets_by_recording_day_with_local_labels(auth_client, env):
    now = datetime.now(UTC)
    # A run today that fetched old footage must not land in today's bar.
    run = env.repository.start_run(
        device_id=env.device_id,
        trigger="backfill",
        window_from_utc=now - timedelta(days=3),
        window_to_utc=now,
    )
    env.repository.finish_run(run, outcome="success", downloaded=1, bytes_archived=999)
    rec_id, _ = env.repository.discover_recording(
        device_id=env.device_id, channel=0, recording=make_recording("today.mp4", now)
    )
    env.repository.finalize_archived(
        rec_id, vault_path="x.enc", plaintext_sha256="0", plaintext_size=500, ciphertext_size=520
    )

    body = auth_client.get("/api/v1/growth", params={"days": 7}).json()

    today = datetime.now(ZoneInfo("Europe/Brussels")).date()
    assert [d["day"] for d in body["days"]][-1] == today.isoformat()
    assert len(body["days"]) == 7
    assert body["days"][-1] == {"day": today.isoformat(), "bytes": 500, "clips": 1}
    assert body["total_bytes"] == 500
    assert body["avg_per_day"] == 500 / 7
