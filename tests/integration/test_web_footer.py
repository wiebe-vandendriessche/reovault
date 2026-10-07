"""The version footer's data: the dashboard renders it from
`GET /api/v1/session`, which is public so the login screen can show it
too. It carries the app version and the pinned reolink-cli version, and no
device at all (the device selector is where that lives)."""

from __future__ import annotations

from reovault import __version__
from tests.integration.conftest import login


def test_session_carries_app_and_pinned_cli_versions(web_settings, client):
    body = client.get("/api/v1/session").json()

    assert body["version"] == __version__
    assert body["pinned_cli_version"] == web_settings.reolink_cli.pinned_version
    assert body["pinned_cli_version"]


def test_versions_are_the_same_logged_in_or_not(client):
    """Public on purpose: the footer is identical before and after login."""
    anon = client.get("/api/v1/session").json()

    authed = login(client).get("/api/v1/session").json()

    assert authed["authenticated"] is True
    assert (authed["version"], authed["pinned_cli_version"]) == (
        anon["version"],
        anon["pinned_cli_version"],
    )


def test_session_never_names_a_device(env, auth_client):
    env.repository.upsert_device(
        alias="doorbell", channel=0, timezone="Europe/Brussels", name="Front door"
    )

    text = auth_client.get("/api/v1/session").text

    assert "doorbell" not in text
    assert "Front door" not in text
