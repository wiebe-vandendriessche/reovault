"""Footage API: day view hour buckets (DST included), type and problem
filters, the per-hour keyset-paginated clip list, single recordings, their
retry/verify actions, and the Health page's "today" summary."""

from datetime import UTC, datetime, timedelta

from reovault.models import ErrorClass, RemoteRecording
from reovault.timeline import local_day_bounds
from reovault.web.api.common import local_today
from tests.integration.conftest import make_recording


def _seed(
    env,
    *,
    name: str,
    start: datetime,
    state: str = "archived",
    rec_type: str | None = "md",
    size: int = 1024,
    device_id: int | None = None,
) -> int:
    """A row in `state` without touching the vault (`vault_path` is fake)."""
    repo = env.repository
    rid, _ = repo.discover_recording(
        device_id=device_id or env.device_id,
        channel=0,
        recording=make_recording(name, start, rec_type=rec_type, size=size, duration_s=30.0),
    )
    if state == "discovered":
        return rid
    repo.transition_downloading(rid)
    if state == "failed":
        repo.mark_failed(
            rid, error="exit code -2", error_class=ErrorClass.DEVICE, next_attempt_at=None
        )
    elif state == "quarantined":
        repo.mark_quarantined(rid, error="sha mismatch", error_class=ErrorClass.PROTOCOL)
    elif state == "archived":
        repo.transition_verifying(rid)
        repo.finalize_archived(
            rid,
            vault_path=f"{name}.enc",
            plaintext_sha256="0" * 64,
            plaintext_size=size,
            ciphertext_size=size,
        )
    return rid


def _archive_real(env, *, name: str, start: datetime, plaintext: bytes = b"x" * 100) -> int:
    staging = env.tmp_path / f"{name}.bin"
    staging.write_bytes(plaintext)
    rec = RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=len(plaintext),
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.transition_verifying(rid)
    result = env.vault.put(staging, device_alias="doorbell", start_utc=start, remote_name=name)
    env.repository.finalize_archived(
        rid,
        vault_path=result.vault_path,
        plaintext_sha256=result.plaintext_sha256,
        plaintext_size=result.plaintext_size,
        ciphertext_size=result.ciphertext_size,
    )
    return rid


def _hours(body: dict) -> dict[int, int]:
    return {h["hour"]: h["total"] for h in body["hours"]}


# -- day view ---------------------------------------------------------------


def test_day_with_two_clips_lists_only_the_two_occupied_hours(env, auth_client):
    # Europe/Brussels is UTC+2 in September (CEST): 08:00 and 20:00 local.
    _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, 0, tzinfo=UTC))
    _seed(env, name="b", start=datetime(2026, 9, 16, 18, 0, 0, tzinfo=UTC))

    r = auth_client.get("/api/v1/day", params={"date_": "2026-09-16"})

    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-09-16"
    assert body["hours"] == [
        {"hour": 8, "total": 1, "bytes": 1024},
        {"hour": 20, "total": 1, "bytes": 1024},
    ]
    assert body["total"] == 2
    assert body["total_bytes"] == 2048
    assert body["available_types"] == ["md"]


def test_empty_day_has_no_hours(env, auth_client):
    body = auth_client.get("/api/v1/day", params={"date_": "2026-09-16"}).json()
    assert body["hours"] == []
    assert body["total"] == 0


def test_day_bytes_count_only_archived_plaintext(env, auth_client):
    _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC), size=500)
    _seed(env, name="b", start=datetime(2026, 9, 16, 6, 10, tzinfo=UTC), state="failed")
    _seed(env, name="c", start=datetime(2026, 9, 16, 6, 20, tzinfo=UTC), state="discovered")

    body = auth_client.get("/api/v1/day", params={"date_": "2026-09-16"}).json()
    assert body["hours"] == [{"hour": 8, "total": 3, "bytes": 500}]


def test_invalid_date_is_422(auth_client):
    r = auth_client.get("/api/v1/day", params={"date_": "16-09-2026"})
    assert r.status_code == 422


def test_dst_fall_back_folds_both_0230s_into_local_hour_2(env, auth_client):
    """2026-10-25, Europe/Brussels: 03:00 CEST becomes 02:00 CET at 01:00 UTC,
    so 00:30 UTC and 01:30 UTC are both 02:30 local."""
    first = _seed(env, name="a", start=datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    second = _seed(env, name="b", start=datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    # 23:30 UTC on the 25th is 00:30 CET on the 26th: not this day.
    _seed(env, name="c", start=datetime(2026, 10, 25, 23, 30, tzinfo=UTC))
    # 22:30 UTC on the 24th is 00:30 CEST on the 25th: this day.
    _seed(env, name="d", start=datetime(2026, 10, 24, 22, 30, tzinfo=UTC))

    day = auth_client.get("/api/v1/day", params={"date_": "2026-10-25"}).json()
    assert _hours(day) == {0: 1, 2: 2}

    hour = auth_client.get("/api/v1/day/hour", params={"date_": "2026-10-25", "hour": 2}).json()
    assert [i["id"] for i in hour["items"]] == [first, second]
    assert [i["local_time"] for i in hour["items"]] == ["02:30", "02:30"]
    assert hour["next"] is None


def test_dst_spring_forward_has_no_local_hour_2(env, auth_client):
    """2026-03-29, Europe/Brussels: 02:00 CET jumps to 03:00 CEST at 01:00 UTC."""
    _seed(env, name="a", start=datetime(2026, 3, 29, 0, 30, tzinfo=UTC))  # 01:30 CET
    _seed(env, name="b", start=datetime(2026, 3, 29, 1, 30, tzinfo=UTC))  # 03:30 CEST

    day = auth_client.get("/api/v1/day", params={"date_": "2026-03-29"}).json()
    assert _hours(day) == {1: 1, 3: 1}
    for h in (1, 3):
        hour = auth_client.get("/api/v1/day/hour", params={"date_": "2026-03-29", "hour": h})
        assert len(hour.json()["items"]) == 1


def test_type_filter_is_a_token_match_and_recomputes_hour_totals(env, auth_client):
    a = _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC), rec_type="md,people")
    _seed(env, name="b", start=datetime(2026, 9, 16, 6, 5, tzinfo=UTC), rec_type="md")
    _seed(env, name="c", start=datetime(2026, 9, 16, 6, 10, tzinfo=UTC), rec_type="peoplecount")
    d = _seed(env, name="d", start=datetime(2026, 9, 16, 18, 0, tzinfo=UTC), rec_type="people")

    unfiltered = auth_client.get("/api/v1/day", params={"date_": "2026-09-16"}).json()
    assert _hours(unfiltered) == {8: 3, 20: 1}
    assert unfiltered["available_types"] == ["md", "people", "peoplecount"]

    day = auth_client.get("/api/v1/day", params={"date_": "2026-09-16", "types": "people"}).json()
    assert _hours(day) == {8: 1, 20: 1}
    assert day["total"] == 2
    assert day["total_bytes"] == 2048
    # The chip vocabulary is the whole day's, not the filtered subset's.
    assert day["available_types"] == unfiltered["available_types"]

    hour8 = auth_client.get(
        "/api/v1/day/hour", params={"date_": "2026-09-16", "hour": 8, "types": "people"}
    ).json()
    assert [i["id"] for i in hour8["items"]] == [a]
    assert hour8["items"][0]["types"] == ["md", "people"]
    hour20 = auth_client.get(
        "/api/v1/day/hour", params={"date_": "2026-09-16", "hour": 20, "types": "people"}
    ).json()
    assert [i["id"] for i in hour20["items"]] == [d]


def test_problems_filter_keeps_only_failed_and_quarantined(env, auth_client):
    start = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
    _seed(env, name="ok", start=start)
    failed = _seed(env, name="f", start=start + timedelta(minutes=1), state="failed")
    quarantined = _seed(env, name="q", start=start + timedelta(minutes=2), state="quarantined")
    _seed(env, name="new", start=start + timedelta(minutes=3), state="discovered")

    params = {"date_": "2026-09-16", "types": "__problems__"}
    day = auth_client.get("/api/v1/day", params=params).json()
    assert day["hours"] == [{"hour": 8, "total": 2, "bytes": 0}]

    hour = auth_client.get("/api/v1/day/hour", params={**params, "hour": 8}).json()
    assert [i["id"] for i in hour["items"]] == [failed, quarantined]
    assert [i["state"] for i in hour["items"]] == ["failed", "quarantined"]


def test_hour_list_keyset_paginates_200_at_a_time_with_id_tiebreak(env, auth_client):
    """201 clips in one local hour, pairs sharing a start_utc so the page
    boundary (items 199/200) falls between two rows with the same start:
    only the `(start_utc, id)` cursor gets that right."""
    base = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
    ids = [
        _seed(
            env, name=f"c{i:03d}", start=base + timedelta(seconds=(i + 1) // 2), state="discovered"
        )
        for i in range(201)
    ]
    params = {"date_": "2026-09-16", "hour": 8}

    page1 = auth_client.get("/api/v1/day/hour", params=params).json()
    assert len(page1["items"]) == 200
    assert page1["next"] is not None
    page2 = auth_client.get("/api/v1/day/hour", params={**params, "after": page1["next"]}).json()
    assert page2["next"] is None

    assert [i["id"] for i in page1["items"] + page2["items"]] == ids
    assert page1["items"][-1]["start_utc"] == page2["items"][0]["start_utc"]


def test_hour_list_ignores_other_hours_and_devices(env, auth_client):
    from reovault.providers.fake import FakeProvider

    _, other = env.archiver_for("garage", FakeProvider())
    mine = _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    _seed(env, name="b", start=datetime(2026, 9, 16, 7, 0, tzinfo=UTC))
    _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC), device_id=other)

    hour = auth_client.get("/api/v1/day/hour", params={"date_": "2026-09-16", "hour": 8}).json()
    assert [i["id"] for i in hour["items"]] == [mine]


# -- single recordings ------------------------------------------------------


def test_get_recording_renders_in_its_devices_timezone(env, auth_client):
    rid = _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC), rec_type="md,people")

    body = auth_client.get(f"/api/v1/recordings/{rid}").json()
    assert body["id"] == rid
    assert body["local_time"] == "08:00"
    assert body["local_datetime"] == "2026-09-16 08:00:00 CEST"
    assert body["types"] == ["md", "people"]
    assert body["state"] == "archived"


def test_unknown_recording_is_404(auth_client):
    assert auth_client.get("/api/v1/recordings/999999").status_code == 404
    assert auth_client.post("/api/v1/recordings/999999/retry").status_code == 404
    assert auth_client.post("/api/v1/recordings/999999/verify").status_code == 404


def test_retry_requeues_a_failed_recording_and_keeps_its_error(env, auth_client):
    rid = _seed(env, name="a", start=datetime(2026, 9, 18, 20, 59, tzinfo=UTC), state="failed")
    env.repository.mark_failed(
        rid, error="exit code -2", error_class=ErrorClass.DEVICE, next_attempt_at=None
    )

    r = auth_client.post(f"/api/v1/recordings/{rid}/retry")

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["state"] == "failed"
    assert body["attempts"] == 0
    assert body["next_attempt_at"] is not None  # eligible for the next run
    # The explanation stays attached until a run actually retries it.
    assert body["last_error"] == "exit code -2"
    assert body["last_error_class_text"] == "The camera reported a problem on its end."


def test_retry_on_an_archived_recording_is_a_no_op(env, auth_client):
    rid = _seed(env, name="a", start=datetime(2026, 9, 18, 20, 59, tzinfo=UTC))
    body = auth_client.post(f"/api/v1/recordings/{rid}/retry").json()
    assert body["state"] == "archived"


def test_retry_requires_csrf(env, auth_client):
    rid = _seed(env, name="a", start=datetime(2026, 9, 18, 20, 59, tzinfo=UTC), state="failed")
    r = auth_client.post(f"/api/v1/recordings/{rid}/retry", headers={"X-CSRF-Token": ""})
    assert r.status_code == 403


def test_verify_reports_ok_then_corruption(env, auth_client):
    rid = _archive_real(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))

    r = auth_client.post(f"/api/v1/recordings/{rid}/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["recording"]["id"] == rid

    vault_file = env.tmp_path / "vault" / env.repository.get(rid).vault_path
    data = bytearray(vault_file.read_bytes())
    data[-1] ^= 0xFF
    vault_file.write_bytes(bytes(data))
    assert auth_client.post(f"/api/v1/recordings/{rid}/verify").json()["ok"] is False


def test_verify_without_a_vault_file_is_404(env, auth_client):
    rid = _seed(env, name="a", start=datetime(2026, 9, 16, 6, 0, tzinfo=UTC), state="failed")
    assert auth_client.post(f"/api/v1/recordings/{rid}/verify").status_code == 404


# -- Health "today" ---------------------------------------------------------


def test_health_today_matches_the_old_row_scan(env, auth_client):
    """`window_summary` replaced a Python sum over `recordings_in_window`:
    count every clip in the local day in any state, bytes only of archived
    plaintext. Rows just outside the local day, or on another device, count
    for nothing."""
    from reovault.providers.fake import FakeProvider

    tz = "Europe/Brussels"
    today = local_today(tz)
    day_start, day_end = local_day_bounds(today, tz)
    _seed(env, name="first", start=day_start, size=100)  # inclusive lower bound
    _seed(env, name="a", start=day_start + timedelta(hours=1), size=200)
    _seed(env, name="f", start=day_start + timedelta(hours=2), state="failed", size=400)
    _seed(env, name="q", start=day_start + timedelta(hours=3), state="quarantined", size=800)
    _seed(env, name="n", start=day_start + timedelta(hours=4), state="discovered", size=1600)
    _seed(env, name="before", start=day_start - timedelta(seconds=1), size=3200)
    _seed(env, name="end", start=day_end, size=6400)  # exclusive upper bound
    _, other = env.archiver_for("garage", FakeProvider())
    _seed(env, name="other", start=day_start + timedelta(hours=1), size=12800, device_id=other)

    rows = env.repository.recordings_in_window(
        device_id=env.device_id, from_utc=day_start, to_utc=day_end, limit=100000
    )
    old_count = len(rows)
    old_bytes = sum(r.plaintext_size or 0 for r in rows if r.state == "archived")
    assert (old_count, old_bytes) == (5, 300)

    r = auth_client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["today"] == {
        "date": today.isoformat(),
        "count": old_count,
        "archived_bytes": old_bytes,
    }


def test_health_today_is_zero_on_an_empty_day(auth_client):
    today = auth_client.get("/api/v1/health").json()["today"]
    assert (today["count"], today["archived_bytes"]) == (0, 0)


def test_day_defaults_to_the_cameras_local_today(auth_client):
    """Footage opens without a date: the server answers for the camera's own
    today rather than 422, so the dashboard needn't know the timezone."""
    body = auth_client.get("/api/v1/day").json()
    assert body["date"] == body["today"]
