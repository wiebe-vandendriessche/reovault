"""Alerts: open after the grace period, dedupe while open, re-notify on the
interval, resolve once, retry an alert nobody received, and never let a
broken channel or URL leak or crash anything."""

from datetime import UTC, datetime, timedelta

import pytest

from reovault.config import AlertRules, AlertsConfig, Settings
from reovault.models import ErrorClass
from reovault.notify import (
    Channel,
    Message,
    Notifier,
    NtfyChannel,
    build_channels,
)
from reovault.providers.fake import FakeProvider
from tests.integration.conftest import make_recording

T0 = datetime(2026, 10, 7, 12, tzinfo=UTC)


class Recorder(Channel):
    name = "recorder"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Message] = []
        self.fail = fail

    def send(self, msg: Message) -> None:
        if self.fail:
            raise OSError("boom")
        self.sent.append(msg)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def fleet(env, web_settings):
    from reovault.fleet import Fleet
    from reovault.providers.gateway import GatewaySupervisor

    f = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    f.archivers[env.device_id] = env.archiver(FakeProvider())
    return f


def _notifier(env, web_settings, fleet, *channels, clock=None, published=None):
    return Notifier(
        settings=web_settings,
        repository=env.repository,
        fleet=fleet,
        channels=list(channels),
        clock=clock or Clock(),
        publish=(lambda t, **d: published.append(t)) if published is not None else None,
    )


def _fail_one(env):
    rec_id, _ = env.repository.discover_recording(
        device_id=env.device_id, channel=0, recording=make_recording()
    )
    env.repository.mark_failed(
        rec_id, error="nope", error_class=ErrorClass.NETWORK, next_attempt_at=None
    )
    return rec_id


def test_nothing_wrong_sends_nothing(env, web_settings, fleet):
    ch = Recorder()
    _notifier(env, web_settings, fleet, ch).evaluate()
    assert ch.sent == []
    assert env.repository.list_alerts() == []


def test_problem_opens_once_then_dedupes_then_renotifies_then_resolves(env, web_settings, fleet):
    ch, clock, published = Recorder(), Clock(), []
    n = _notifier(env, web_settings, fleet, ch, clock=clock, published=published)
    rec_id = _fail_one(env)

    n.evaluate()
    assert [m.title for m in ch.sent] == ["doorbell: 1 recording needs attention"]
    assert published == ["alert"]

    clock.now += timedelta(hours=1)
    n.evaluate()
    assert len(ch.sent) == 1  # still open, inside renotify_hours: no repeat

    clock.now += timedelta(hours=24)
    n.evaluate()
    assert len(ch.sent) == 2  # renotified

    env.repository.retry_recording(rec_id)
    env.repository.conn.execute("UPDATE recordings SET state='archived' WHERE id=?", (rec_id,))
    n.evaluate()
    assert ch.sent[-1].resolved
    assert ch.sent[-1].title.startswith("Resolved: ")
    assert env.repository.list_alerts() == []


def test_alert_nobody_received_is_retried(env, web_settings, fleet):
    broken = Recorder(fail=True)
    n = _notifier(env, web_settings, fleet, broken)
    _fail_one(env)
    n.evaluate()
    (row,) = env.repository.list_alerts()
    assert row.last_sent_at is None  # every channel failed: still unsent

    broken.fail = False
    n.evaluate()
    assert len(broken.sent) == 1
    assert env.repository.list_alerts()[0].last_sent_at is not None


def test_one_failing_channel_does_not_block_the_other(env, web_settings, fleet):
    good, bad = Recorder(), Recorder(fail=True)
    n = _notifier(env, web_settings, fleet, bad, good)
    results = n.send(Message("t", "b"))
    assert [(r.channel, r.ok) for r in results] == [("recorder", False), ("recorder", True)]
    assert len(good.sent) == 1


def test_disabling_rules_clears_open_alerts_without_a_resolve_message(env, web_settings, fleet):
    ch = Recorder()
    live = {"settings": web_settings}
    n = Notifier(
        settings=lambda: live["settings"],
        repository=env.repository,
        fleet=fleet,
        channels=[ch],
        clock=Clock(),
    )
    _fail_one(env)
    n.evaluate()
    # The rules are read live, as after an edit of reovault.toml.
    alerts = web_settings.alerts.model_copy(update={"rules": AlertRules(enabled=False)})
    live["settings"] = web_settings.model_copy(update={"alerts": alerts})
    n.evaluate()
    assert len(ch.sent) == 1
    assert env.repository.list_alerts() == []


def test_gateway_down_waits_out_its_grace_period(env, web_settings, fleet, monkeypatch):
    from reovault import notify
    from reovault.notify import current_conditions as real_conditions

    def fake_conditions(fleet_, repo, rules):
        return [
            notify.Condition(
                "gateway_down", None, "Gateway down", "down", grace=timedelta(minutes=15)
            )
        ]

    monkeypatch.setattr(notify, "current_conditions", fake_conditions)
    ch, clock = Recorder(), Clock()
    n = _notifier(env, web_settings, fleet, ch, clock=clock)
    n.evaluate()
    clock.now += timedelta(minutes=10)
    n.evaluate()
    assert ch.sent == []
    clock.now += timedelta(minutes=6)
    n.evaluate()
    assert [m.title for m in ch.sent] == ["Gateway down"]
    assert real_conditions is not fake_conditions


def test_channels_are_built_only_when_configured(tmp_path):
    assert build_channels(AlertsConfig()) == []
    secret = tmp_path / "hook"
    secret.write_text("https://example.invalid/hook\n")
    names = [
        c.name
        for c in build_channels(
            AlertsConfig(
                ntfy_url="https://ntfy.sh/x",
                webhook_url_file=secret,
                smtp_host="mail",
                smtp_to=["a@b.c"],
            )
        )
    ]
    assert names == ["ntfy", "webhook", "email"]


def test_non_http_channel_url_is_refused():
    with pytest.raises(ValueError, match="http"):
        NtfyChannel("file:///etc/passwd", None).send(Message("t", "b"))


def test_alerts_api_rules_and_test_send(auth_client, web_app, env):
    st = web_app.state.rv
    body = auth_client.get("/api/v1/alerts").json()
    assert body["rules"]["enabled"] is True
    assert {c["name"] for c in body["channels"]} == {"ntfy", "webhook", "email"}
    assert auth_client.post("/api/v1/alerts/test").status_code == 409  # nothing configured

    ch = Recorder()
    st.notifier.channels = [ch]
    results = auth_client.post("/api/v1/alerts/test").json()
    assert results == [{"channel": "recorder", "ok": True, "error": None}]

    rules = {**body["rules"], "renotify_hours": 6}
    put = auth_client.put("/api/v1/alerts/rules", json=rules)
    assert put.status_code == 200
    assert put.json()["rules"]["renotify_hours"] == 6


def test_alerts_rules_env_pinned(auth_client, monkeypatch):
    monkeypatch.setenv("REOVAULT_ALERTS__RULES__ENABLED", "true")
    body = auth_client.get("/api/v1/alerts").json()
    assert body["env_pinned"] is True
    assert auth_client.put("/api/v1/alerts/rules", json=body["rules"]).status_code == 409


def test_secrets_never_live_in_settings_dump(tmp_path):
    """Channel config holds file paths, never the secret values."""
    secret = tmp_path / "token"
    secret.write_text("tk_supersecret")
    s = Settings(alerts=AlertsConfig(ntfy_url="https://ntfy.sh/x", ntfy_token_file=secret))
    assert "tk_supersecret" not in s.model_dump_json()
