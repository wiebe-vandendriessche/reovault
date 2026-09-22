"""Footage day view: empty hours are not rendered."""

from reovault.models import RemoteRecording


def _archive_one(env, *, name: str, start):
    rec = RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=30.0,
        rec_type="md",
        stream="main",
        remote_size=1024,
        raw_metadata={"fileName": name},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.transition_verifying(rid)
    env.repository.finalize_archived(
        rid,
        vault_path=f"{name}.enc",
        plaintext_sha256="0" * 64,
        plaintext_size=1024,
        ciphertext_size=1024,
    )


def test_day_with_two_clips_shows_only_the_two_occupied_hours(env, auth_client):
    from datetime import UTC, datetime

    # Europe/Brussels is UTC+2 in September (CEST): 08:00 and 20:00 local.
    _archive_one(env, name="a", start=datetime(2026, 9, 16, 6, 0, 0, tzinfo=UTC))
    _archive_one(env, name="b", start=datetime(2026, 9, 16, 18, 0, 0, tzinfo=UTC))

    r = auth_client.get("/fragments/day", params={"date_": "2026-09-16"})
    assert r.status_code == 200

    # The two occupied hours render as real disclosures...
    assert 'id="h-8"' in r.text
    assert 'id="h-20"' in r.text
    # ...and none of the other 22 empty hours render anything at all: no
    # leftover "quiet" row markup, no id for an hour with zero clips.
    for h in range(24):
        if h in (8, 20):
            continue
        assert f'id="h-{h}"' not in r.text
    assert "quiet" not in r.text.lower()

    # The one summary line names exactly how many were hidden.
    assert "22 hours with no recordings are not shown." in r.text


def test_day_with_one_clip_uses_singular_hidden_hours_wording(env, auth_client):
    from datetime import UTC, datetime

    _archive_one(env, name="a", start=datetime(2026, 9, 16, 6, 0, 0, tzinfo=UTC))

    r = auth_client.get("/fragments/day", params={"date_": "2026-09-16"})
    assert "23 hours with no recordings are not shown." in r.text
