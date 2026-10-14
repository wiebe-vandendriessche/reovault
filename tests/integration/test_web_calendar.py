"""The Footage month calendar, and the `?device=` scoping every
device-scoped endpoint shares."""

from datetime import UTC, datetime, timedelta

from reovault.models import ErrorClass
from reovault.providers.fake import FakeProvider
from tests.integration.conftest import make_recording


def _seed(env, start: datetime, *, failed: bool = False, device_id: int | None = None) -> int:
    rid, _ = env.repository.discover_recording(
        device_id=device_id or env.device_id,
        channel=0,
        recording=make_recording(f"{start:%Y%m%d%H%M%S}", start),
    )
    if failed:
        env.repository.transition_downloading(rid)
        env.repository.mark_failed(
            rid, error="x", error_class=ErrorClass.NETWORK, next_attempt_at=None
        )
    return rid


def test_calendar_lists_only_days_with_clips_and_scales_density(env, auth_client):
    base = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    for i in range(8):
        _seed(env, base + timedelta(minutes=i))
    _seed(env, datetime(2026, 10, 6, 10, 0, tzinfo=UTC), failed=True)
    for i in range(4):
        _seed(env, datetime(2026, 10, 7, 10, i, tzinfo=UTC))

    r = auth_client.get("/api/v1/calendar", params={"month": "2026-10"})

    assert r.status_code == 200
    body = r.json()
    assert body["month"] == "2026-10"
    assert body["days"] == [
        {"date": "2026-10-05", "count": 8, "problems": 0, "density": 4},
        # 1/8 of the peak rounds to 0 but a day with a clip is never blank.
        {"date": "2026-10-06", "count": 1, "problems": 1, "density": 1},
        {"date": "2026-10-07", "count": 4, "problems": 0, "density": 2},
    ]


def test_calendar_month_edges_follow_local_days(env, auth_client):
    """22:30 UTC on 30 Sep is 00:30 CEST on 1 Oct: October's clip, not
    September's. 22:30 UTC on 31 Oct is 23:30 CET, still October."""
    _seed(env, datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    _seed(env, datetime(2026, 10, 31, 22, 30, tzinfo=UTC))
    _seed(env, datetime(2026, 10, 31, 23, 30, tzinfo=UTC))  # 00:30 CET on 1 Nov

    october = auth_client.get("/api/v1/calendar", params={"month": "2026-10"}).json()
    assert [d["date"] for d in october["days"]] == ["2026-10-01", "2026-10-31"]
    september = auth_client.get("/api/v1/calendar", params={"month": "2026-09"}).json()
    assert september["days"] == []
    november = auth_client.get("/api/v1/calendar", params={"month": "2026-11"}).json()
    assert [d["date"] for d in november["days"]] == ["2026-11-01"]


def test_december_rolls_into_next_year(env, auth_client):
    _seed(env, datetime(2026, 12, 31, 12, 0, tzinfo=UTC))
    body = auth_client.get("/api/v1/calendar", params={"month": "2026-12"}).json()
    assert [d["date"] for d in body["days"]] == ["2026-12-31"]


def test_malformed_month_is_422(auth_client):
    for month in ("2026-13", "2026-1", "202610", "", "2026-00"):
        r = auth_client.get("/api/v1/calendar", params={"month": month})
        assert r.status_code == 422, month


# -- device scoping ---------------------------------------------------------


def test_device_param_is_optional_with_one_enabled_camera(env, auth_client):
    _seed(env, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))
    implicit = auth_client.get("/api/v1/calendar", params={"month": "2026-10"}).json()
    explicit = auth_client.get(
        "/api/v1/calendar", params={"month": "2026-10", "device": env.device_id}
    ).json()
    assert implicit == explicit
    assert len(implicit["days"]) == 1


def test_device_param_is_required_with_several_cameras(env, web_app, auth_client):
    archiver, other = env.archiver_for("garage", FakeProvider())
    web_app.state.rv.fleet.archivers[other] = archiver
    _seed(env, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))
    _seed(env, datetime(2026, 10, 9, 10, 0, tzinfo=UTC), device_id=other)

    for path, params in (
        ("/api/v1/calendar", {"month": "2026-10"}),
        ("/api/v1/day", {"date_": "2026-10-05"}),
        ("/api/v1/day/hour", {"date_": "2026-10-05", "hour": 12}),
        ("/api/v1/problems", {}),
        ("/api/v1/problems/count", {}),
        ("/api/v1/health", {}),
    ):
        assert auth_client.get(path, params=params).status_code == 400, path

    garage = auth_client.get("/api/v1/calendar", params={"month": "2026-10", "device": other})
    assert [d["date"] for d in garage.json()["days"]] == ["2026-10-09"]


def test_unknown_device_is_404(auth_client):
    r = auth_client.get("/api/v1/calendar", params={"month": "2026-10", "device": 999})
    assert r.status_code == 404


def test_device_in_repo_but_not_enabled_is_404(env, auth_client):
    _, disabled = env.archiver_for("garage", FakeProvider())
    r = auth_client.get("/api/v1/day", params={"date_": "2026-10-05", "device": disabled})
    assert r.status_code == 404
