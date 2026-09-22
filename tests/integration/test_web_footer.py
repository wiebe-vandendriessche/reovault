"""The version footer (see base.html): every authenticated page gets it now,
not just Health, and it carries no device at all any more -- the navbar's
own device selector is where that lives now."""

from __future__ import annotations

from reovault import __version__


def _footer(html: str) -> str:
    return html.split('class="app-footer"', 1)[1]


def test_footer_appears_on_every_authenticated_page(env, auth_client):
    for path in ("/", "/schedule", "/devices", "/recordings", "/runs", "/problems"):
        r = auth_client.get(path)
        assert r.status_code == 200, path
        assert 'class="app-footer"' in r.text, path
        footer = _footer(r.text)
        assert __version__ in footer, path
        assert "reolink-cli" in footer, path


def test_footer_never_names_a_device(env, auth_client):
    r = auth_client.get("/")

    assert "doorbell" not in _footer(r.text)


def test_theme_toggle_button_is_present_and_left_of_sign_out(env, auth_client):
    r = auth_client.get("/")

    assert 'id="theme-toggle"' in r.text
    toggle_pos = r.text.index('id="theme-toggle"')
    sign_out_pos = r.text.index('hx-post="/logout"')
    assert toggle_pos < sign_out_pos


def test_navbar_uses_the_devices_own_name_when_set(env, auth_client):
    env.repository.upsert_device(
        alias="doorbell", channel=0, timezone="Europe/Brussels", name="Front door"
    )

    r = auth_client.get("/")

    assert "Front door" in r.text
    assert "doorbell" not in _footer(r.text)
