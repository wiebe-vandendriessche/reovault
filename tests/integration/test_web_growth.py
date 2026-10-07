"""Vault growth: the series is the last N camera-local days, today included,
labelled with local dates."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def test_growth_includes_today_with_local_labels(auth_client, env):
    run = env.repository.start_run(
        device_id=env.device_id,
        trigger="manual",
        window_from_utc=datetime.now(UTC) - timedelta(hours=1),
        window_to_utc=datetime.now(UTC),
    )
    env.repository.finish_run(run, outcome="success", downloaded=2, bytes_archived=500)
    body = auth_client.get("/api/v1/growth", params={"days": 7}).json()
    today = datetime.now(ZoneInfo("Europe/Brussels")).date()
    assert [d["day"] for d in body["days"]][-1] == today.isoformat()
    assert len(body["days"]) == 7
    assert body["days"][-1]["bytes"] == 500
    assert body["avg_per_day"] == 500 / 7
