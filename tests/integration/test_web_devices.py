"""The Devices API: listing, discovery, adding a camera, enable/disable, and
the end-to-end version of the password-never-leaked invariant: a real
add-camera POST through the whole web stack, with a stubbed `reolink-cli`
subprocess, must never put the password in the response.
"""

import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from reovault.models import DiscoveredDevice, ErrorClass, RegisteredDevice
from reovault.providers.base import ProviderError
from reovault.providers.fake import FakeRegistry
from reovault.providers.reolink_cli_registry import ReolinkCliRegistry
from tests.integration.conftest import login, make_web_app

SECRET_PASSWORD = "hunter2-camera-secret"  # noqa: S105 - test fixture

ADD_BODY = {
    "alias": "back-yard",
    "name": "Back yard",
    "host": "192.168.1.99",
    "user": "admin",
    "password": SECRET_PASSWORD,
    "timezone": "Europe/Brussels",
}


def _client(env, web_settings, web_device, **kwargs) -> TestClient:
    return login(TestClient(make_web_app(env, web_settings, web_device, **kwargs)))


def test_devices_lists_the_seeded_device(env, auth_client):
    r = auth_client.get("/api/v1/devices")

    assert r.status_code == 200
    body = r.json()
    assert [d["id"] for d in body["devices"]] == [env.device_id]
    device = body["devices"][0]
    assert device["alias"] == "doorbell"
    assert device["name"] is None  # the SPA renders its own placeholder
    assert device["enabled"] is True
    assert device["sample"] is None
    assert device["sd"] is None


def test_sd_card_bar_has_two_segments_archived_and_other(env, auth_client):
    """Regression: a third "not yet archived" bucket used to sit between
    archived and free. It was dropped, leaving archived and everything else
    used."""
    from datetime import UTC, datetime

    from reovault.models import RemoteRecording

    env.repository.record_storage_sample(
        device_id=env.device_id,
        total_gb=100.0,
        remain_gb=20.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=None,
    )
    rec = RemoteRecording(
        remote_name="clip1",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=None,
        duration_s=30.0,
        rec_type="md",
        stream="main",
        remote_size=1_000_000_000,
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.transition_verifying(rid)
    env.repository.finalize_archived(
        rid,
        vault_path="clip1.enc",
        plaintext_sha256="a" * 64,
        plaintext_size=1_000_000_000,
        ciphertext_size=1_000_000_100,
    )

    sd = auth_client.get("/api/v1/devices").json()["devices"][0]["sd"]

    assert set(sd) == {"total_gb", "used_gb", "free_gb", "archived_gb", "other_gb"}
    assert sd["used_gb"] == 80.0
    assert sd["free_gb"] == 20.0
    assert sd["archived_gb"] > 0
    assert abs(sd["archived_gb"] + sd["other_gb"] - sd["used_gb"]) < 1e-9


def test_devices_without_a_registry_reports_it_unavailable(env, auth_client):
    r = auth_client.get("/api/v1/devices")  # registry=None

    assert r.json()["registry_available"] is False


def test_discover_and_add_are_503_without_a_registry(env, auth_client):
    assert auth_client.post("/api/v1/devices/discover").status_code == 503
    assert auth_client.post("/api/v1/devices", json=ADD_BODY).status_code == 503


def test_discover_returns_found_devices(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry(
        discovered=[
            DiscoveredDevice(
                host="192.168.1.99:9000",
                uid="uid-1",
                mac="aa:bb:cc:dd:ee:ff",
                protocol="v20",
                model="D340W",
                name="Back yard",
                already_registered=False,
            )
        ]
    )
    client = _client(env, web_settings, web_device, registry=registry)

    assert client.get("/api/v1/devices").json()["registry_available"] is True
    r = client.post("/api/v1/devices/discover")

    assert r.status_code == 200
    assert r.json() == [
        {
            "host": "192.168.1.99:9000",
            "uid": "uid-1",
            "mac": "aa:bb:cc:dd:ee:ff",
            "protocol": "v20",
            "model": "D340W",
            "name": "Back yard",
            "already_registered": False,
        }
    ]


def test_discover_marks_already_registered_devices(
    env, web_settings, web_device, web_password_hash
):
    registry = FakeRegistry(
        discovered=[
            DiscoveredDevice(
                host="x",
                uid=None,
                mac=None,
                protocol=None,
                model=None,
                name="doorbell",
                already_registered=True,
            )
        ]
    )
    client = _client(env, web_settings, web_device, registry=registry)

    r = client.post("/api/v1/devices/discover")

    assert [d["already_registered"] for d in r.json()] == [True]


def test_discover_provider_error_is_502(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry()
    client = _client(env, web_settings, web_device, registry=registry)

    with patch.object(registry, "discover", side_effect=ProviderError("boom", ErrorClass.NETWORK)):
        r = client.post("/api/v1/devices/discover")

    assert r.status_code == 502


def test_add_device_registers_it_with_the_registry_and_the_repository(
    env, web_settings, web_device, web_password_hash
):
    registry = FakeRegistry()
    client = _client(env, web_settings, web_device, registry=registry)

    r = client.post("/api/v1/devices", json=ADD_BODY)

    assert r.status_code == 201
    added = [d for d in r.json()["devices"] if d["alias"] == "back-yard"]
    assert len(added) == 1
    assert added[0]["name"] == "Back yard"
    assert added[0]["enabled"] is True
    assert len(registry.received_passwords) == 1
    assert registry.received_passwords[0].get_secret_value() == SECRET_PASSWORD
    device_id = env.repository.get_device_id(alias="back-yard", channel=0)
    assert device_id is not None
    device_row = env.repository.get_device(device_id)
    assert device_row.timezone == "Europe/Brussels"
    assert device_row.enabled is True
    # Enabled in the live fleet too, so it shows in the navbar picker.
    enabled = client.get("/api/v1/devices/enabled").json()
    assert device_id in {d["id"] for d in enabled}


def test_add_device_rejects_a_duplicate_alias(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry(
        devices=[
            RegisteredDevice(
                alias="doorbell",
                host="x",
                user="admin",
                has_password=True,
                channel=0,
                description=None,
                disabled=False,
            )
        ]
    )
    client = _client(env, web_settings, web_device, registry=registry)

    # already registered as env's seeded device
    r = client.post("/api/v1/devices", json={**ADD_BODY, "alias": "doorbell"})

    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]
    assert registry.received_passwords == []  # never even attempted


def test_add_device_rejects_an_unknown_timezone(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry()
    client = _client(env, web_settings, web_device, registry=registry)

    r = client.post("/api/v1/devices", json={**ADD_BODY, "timezone": "Not/A_Real_Zone"})

    assert r.status_code == 422
    assert "Unknown timezone" in r.json()["detail"]
    assert registry.received_passwords == []


def test_add_device_rejects_missing_required_fields(
    env, web_settings, web_device, web_password_hash
):
    registry = FakeRegistry()
    client = _client(env, web_settings, web_device, registry=registry)

    # host/user/password/timezone all missing
    r = client.post("/api/v1/devices", json={"alias": "back-yard"})

    assert r.status_code == 422
    assert registry.received_passwords == []


def test_add_device_registry_failure_is_502(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry()
    client = _client(env, web_settings, web_device, registry=registry)

    with patch.object(
        registry, "add_device", side_effect=ProviderError("boom", ErrorClass.NETWORK)
    ):
        r = client.post("/api/v1/devices", json=ADD_BODY)

    assert r.status_code == 502
    assert env.repository.get_device_id(alias="back-yard", channel=0) is None


def test_set_enabled_disables_and_re_enables_a_device(env, auth_client):
    url = f"/api/v1/devices/{env.device_id}/enabled"

    r_off = auth_client.put(url, json={"enabled": False})
    assert r_off.status_code == 200
    assert env.repository.get_device(env.device_id).enabled is False
    assert r_off.json()["devices"][0]["enabled"] is False
    assert auth_client.get("/api/v1/devices/enabled").json() == []

    # Idempotent: a repeat of the same value is a no-op, not a flip.
    r_again = auth_client.put(url, json={"enabled": False})
    assert r_again.status_code == 200
    assert env.repository.get_device(env.device_id).enabled is False

    r_on = auth_client.put(url, json={"enabled": True})
    assert r_on.status_code == 200
    assert env.repository.get_device(env.device_id).enabled is True
    assert [d["id"] for d in auth_client.get("/api/v1/devices/enabled").json()] == [env.device_id]


def test_set_enabled_unknown_device_is_404(env, auth_client):
    r = auth_client.put("/api/v1/devices/999999/enabled", json={"enabled": False})
    assert r.status_code == 404


# -- end-to-end password safety through the real web stack -----------------


def test_add_device_password_never_appears_in_the_response(
    env, web_settings, web_device, web_password_hash
):
    """The full-stack version of the registry-level password safety tests:
    a real ReolinkCliRegistry, a stubbed reolink-cli subprocess, a real
    HTTP round trip, and the response checked for the plaintext password."""
    registry = ReolinkCliRegistry(binary="reolink-cli")
    ok = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"ok": True, "data": {"action": "added"}, "error": None}),
        stderr="",
    )
    client = _client(env, web_settings, web_device, registry=registry)

    with patch("subprocess.run", return_value=ok) as spy:
        r = client.post("/api/v1/devices", json=ADD_BODY)

    assert SECRET_PASSWORD not in r.text
    # Several subprocess.run calls can happen around this request (e.g. a
    # gateway status check); find the one that's actually `device add`.
    add_calls = [c for c in spy.call_args_list if "add" in c.args[0]]
    assert len(add_calls) == 1
    add_call = add_calls[0]
    assert SECRET_PASSWORD not in add_call.args[0]
    assert all(SECRET_PASSWORD not in str(a) for a in add_call.args[0])
    assert add_call.kwargs.get("input") == SECRET_PASSWORD + "\n"
