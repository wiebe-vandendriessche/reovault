"""The Devices tab: listing, discovery, adding a camera, enable/disable, and
the end-to-end version of the password-never-leaked invariant: a real
add-camera POST through the whole web stack, with a stubbed `reolink-cli`
subprocess, must never put the password in the rendered response.
"""

import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from reovault.models import DiscoveredDevice, RegisteredDevice
from reovault.providers.fake import FakeRegistry
from reovault.providers.reolink_cli_registry import ReolinkCliRegistry
from tests.integration.conftest import extract_body_csrf, login, make_web_app

SECRET_PASSWORD = "hunter2-camera-secret"  # noqa: S105 - test fixture


def test_devices_page_lists_the_seeded_device(env, auth_client):
    r = auth_client.get("/devices")

    assert r.status_code == 200
    # The alias is never shown, only the ReoVault name; env's seeded device
    # has no name set, so it falls back to a plain placeholder rather than
    # leaking the internal reolink-cli alias.
    assert "Unnamed camera" in r.text
    assert "doorbell" not in r.text


def test_sd_card_bar_has_no_pending_segment(env, auth_client):
    """Regression: a third "not yet archived" bucket used to sit between
    archived and free, in --warn yellow. It was dropped -- a user can
    already read "not yet archived" off archived-vs-used -- leaving just
    two segments, archived (blue) and everything else used (grey)."""
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

    r = auth_client.get("/devices")

    assert "not yet archived" not in r.text
    assert "is-pending" not in r.text
    assert "GB archived" in r.text
    assert "GB used" in r.text


def test_devices_page_without_a_registry_shows_the_unavailable_banner(env, auth_client):
    r = auth_client.get("/devices")  # registry=None

    assert "aren't available" in r.text
    assert "Find cameras" not in r.text


def test_discover_renders_found_devices(env, web_settings, web_device, web_password_hash):
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
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post("/devices/discover", headers={"X-CSRF-Token": csrf})

    assert r.status_code == 200
    assert "Back yard" in r.text
    assert "192.168.1.99:9000" in r.text
    assert "Add this camera" in r.text


def test_discover_marks_already_registered_devices_without_an_add_form(
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
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post("/devices/discover", headers={"X-CSRF-Token": csrf})

    assert "Already added" in r.text
    assert "Add this camera" not in r.text


def test_add_device_registers_it_with_the_registry_and_the_repository(
    env, web_settings, web_device, web_password_hash
):
    registry = FakeRegistry()
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post(
        "/devices",
        data={
            "alias": "back-yard",
            "name": "Back yard",
            "host": "192.168.1.99",
            "user": "admin",
            "password": SECRET_PASSWORD,
            "timezone": "Europe/Brussels",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200
    # The alias is never shown, only the ReoVault name.
    assert "Back yard" in r.text
    assert "back-yard" not in r.text
    assert len(registry.received_passwords) == 1
    assert registry.received_passwords[0].get_secret_value() == SECRET_PASSWORD
    device_id = env.repository.get_device_id(alias="back-yard", channel=0)
    assert device_id is not None
    device_row = env.repository.get_device(device_id)
    assert device_row.timezone == "Europe/Brussels"
    assert device_row.enabled is True


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
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post(
        "/devices",
        data={
            "alias": "doorbell",  # already registered as env's seeded device
            "host": "192.168.1.99",
            "user": "admin",
            "password": SECRET_PASSWORD,
            "timezone": "Europe/Brussels",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert "already registered" in r.text
    assert registry.received_passwords == []  # never even attempted


def test_add_device_rejects_an_unknown_timezone(env, web_settings, web_device, web_password_hash):
    registry = FakeRegistry()
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post(
        "/devices",
        data={
            "alias": "back-yard",
            "host": "192.168.1.99",
            "user": "admin",
            "password": SECRET_PASSWORD,
            "timezone": "Not/A_Real_Zone",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert "Unknown timezone" in r.text
    assert registry.received_passwords == []


def test_add_device_rejects_missing_required_fields(
    env, web_settings, web_device, web_password_hash
):
    registry = FakeRegistry()
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    r = client.post(
        "/devices",
        data={"alias": "back-yard"},  # host/user/password/timezone all missing
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200
    assert "field-error" in r.text


def test_toggle_disables_and_re_enables_a_device(env, auth_client):
    client = auth_client
    csrf = extract_body_csrf(client.get("/devices").text)

    r_off = client.post(f"/devices/{env.device_id}/toggle", headers={"X-CSRF-Token": csrf})
    assert env.repository.get_device(env.device_id).enabled is False
    assert r_off.status_code == 200
    assert "checked" not in r_off.text  # the lone device's switch is now unchecked

    r_on = client.post(f"/devices/{env.device_id}/toggle", headers={"X-CSRF-Token": csrf})
    assert env.repository.get_device(env.device_id).enabled is True
    assert r_on.status_code == 200


def test_toggle_unknown_device_is_404(env, auth_client):
    csrf = extract_body_csrf(auth_client.get("/devices").text)

    r = auth_client.post("/devices/999999/toggle", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404


# -- end-to-end password safety through the real web stack -----------------


def test_add_device_password_never_appears_in_the_rendered_response(
    env, web_settings, web_device, web_password_hash
):
    """The full-stack version of the registry-level password safety tests:
    a real ReolinkCliRegistry, a stubbed reolink-cli subprocess, a real
    HTTP round trip, and the rendered HTML checked for the plaintext
    password."""
    registry = ReolinkCliRegistry(binary="reolink-cli")
    ok = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"ok": True, "data": {"action": "added"}, "error": None}),
        stderr="",
    )
    app = make_web_app(env, web_settings, web_device, registry=registry)
    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/devices").text)

    with patch("subprocess.run", return_value=ok) as spy:
        r = client.post(
            "/devices",
            data={
                "alias": "back-yard",
                "host": "192.168.1.99",
                "user": "admin",
                "password": SECRET_PASSWORD,
                "timezone": "Europe/Brussels",
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert SECRET_PASSWORD not in r.text
    # Several subprocess.run calls can happen around this request (e.g. a
    # gateway status check); find the one that's actually `device add`.
    add_calls = [c for c in spy.call_args_list if "add" in c.args[0]]
    assert len(add_calls) == 1
    add_call = add_calls[0]
    assert SECRET_PASSWORD not in add_call.args[0]
    assert all(SECRET_PASSWORD not in str(a) for a in add_call.args[0])
    assert add_call.kwargs.get("input") == SECRET_PASSWORD + "\n"
