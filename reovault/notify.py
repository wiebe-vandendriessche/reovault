"""Alerts: tell someone when archiving needs attention, instead of failing
silently until the dashboard is next opened.

State-based: `Notifier.evaluate()` (a scheduler job, once a minute) computes
which conditions are true right now and diffs them against `alert_state`.
A new condition opens an alert (after its grace period), a condition that
went away resolves it, and an alert still open re-notifies every
`renotify_hours`. A restart therefore neither loses nor repeats anything.

Channels are stdlib only: ntfy and a generic JSON webhook over `urllib`,
email over `smtplib`. A failed send never raises into the caller; an alert
nobody received stays unsent and is retried on the next evaluation.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reovault import health
from reovault.config import AlertRules, AlertsConfig, Settings
from reovault.db.repository import Repository, parse_iso_utc
from reovault.fleet import Fleet
from reovault.logging import get_logger
from reovault.providers.reolink_cli import ReolinkCliProvider

logger = get_logger(__name__)

RULES_SETTING_KEY = "alert_rules"  # pre-0.4 app_settings key, migrated by config_store
_RULES_ENV_PREFIX = "REOVAULT_ALERTS__RULES__"
_TIMEOUT_SECS = 10


# -- rules: `[alerts.rules]` in reovault.toml, editable from the dashboard --


def is_rules_env_pinned() -> bool:
    return any(k.startswith(_RULES_ENV_PREFIX) for k in os.environ)


# -- channels --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    body: str
    resolved: bool = False


class Channel:
    name = ""

    def send(self, msg: Message) -> None:
        raise NotImplementedError


def _read_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _http_url(url: str) -> str:
    """urllib also opens file:// and ftp://; a channel URL must be http(s)."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("alert URLs must be http:// or https://")
    return url


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(_http_url(url), data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:  # noqa: S310 - scheme checked
        resp.read(1024)


class NtfyChannel(Channel):
    name = "ntfy"

    def __init__(self, url: str, token_file: Path | None) -> None:
        self.url, self.token_file = url, token_file

    def send(self, msg: Message) -> None:
        headers = {
            "Title": msg.title,
            "Priority": "default" if msg.resolved else "high",
            "Tags": "white_check_mark" if msg.resolved else "warning",
        }
        if self.token_file is not None:
            headers["Authorization"] = f"Bearer {_read_secret(self.token_file)}"
        _post(self.url, msg.body.encode(), headers)


class WebhookChannel(Channel):
    """Generic JSON POST. `text` and `content` duplicate the message so a
    Slack or Discord incoming-webhook URL works as-is."""

    name = "webhook"

    def __init__(self, url_file: Path) -> None:
        self.url_file = url_file

    def send(self, msg: Message) -> None:
        line = f"{msg.title}: {msg.body}"
        payload = {
            "source": "reovault",
            "title": msg.title,
            "message": msg.body,
            "resolved": msg.resolved,
            "text": line,
            "content": line,
        }
        _post(
            _read_secret(self.url_file),
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )


class SmtpChannel(Channel):
    name = "email"

    def __init__(self, cfg: AlertsConfig) -> None:
        self.cfg = cfg

    def send(self, msg: Message) -> None:
        cfg = self.cfg
        assert cfg.smtp_host is not None
        email = EmailMessage()
        email["Subject"] = f"[ReoVault] {msg.title}"
        email["From"] = cfg.smtp_from or cfg.smtp_user or "reovault@localhost"
        email["To"] = ", ".join(cfg.smtp_to)
        email.set_content(msg.body)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if cfg.smtp_port == 465:
            smtp = smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT_SECS, context=context
            )
        else:
            smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT_SECS)
        with smtp:
            if cfg.smtp_port != 465 and cfg.smtp_starttls:
                smtp.starttls(context=context)
            if cfg.smtp_user and cfg.smtp_password_file is not None:
                smtp.login(cfg.smtp_user, _read_secret(cfg.smtp_password_file))
            smtp.send_message(email)


def build_channels(cfg: AlertsConfig) -> list[Channel]:
    channels: list[Channel] = []
    if cfg.ntfy_url:
        channels.append(NtfyChannel(cfg.ntfy_url, cfg.ntfy_token_file))
    if cfg.webhook_url_file is not None:
        channels.append(WebhookChannel(cfg.webhook_url_file))
    if cfg.smtp_host and cfg.smtp_to:
        channels.append(SmtpChannel(cfg))
    return channels


# -- conditions ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Condition:
    condition: str
    device_id: int | None
    title: str
    message: str
    grace: timedelta = timedelta(0)

    @property
    def key(self) -> str:
        return f"{self.condition}:{self.device_id if self.device_id is not None else '*'}"


def current_conditions(fleet: Fleet, repository: Repository, rules: AlertRules) -> list[Condition]:
    """Everything that is wrong right now, from data that already exists:
    run outcomes, recording states, the storage sample, the gateway."""
    out: list[Condition] = []
    gateway_checked = False
    for device_id in fleet.ids():
        row = repository.get_device(device_id)
        archiver = fleet.get(device_id)
        if row is None or archiver is None:
            continue
        label = row.name or row.alias

        if rules.run_failed:
            last = repository.last_finished_run(device_id)
            if last is not None and last.outcome in ("failed", "aborted"):
                out.append(
                    Condition(
                        "run_failed",
                        device_id,
                        f"{label}: archive run {last.outcome}",
                        f"The last archive run for {label} {last.outcome}. "
                        "New clips may not be getting archived.",
                    )
                )

        if rules.problems:
            totals = repository.totals(device_id)
            n = totals.failed_count + totals.quarantined_count
            if n:
                plural = "s" if n != 1 else ""
                out.append(
                    Condition(
                        "problems",
                        device_id,
                        f"{label}: {n} recording{plural} {'needs' if n == 1 else 'need'} attention",
                        f"{n} recording{plural} from {label} failed or were quarantined.",
                    )
                )

        if rules.coverage:
            cov = health.coverage_margin(repository=repository, device_id=device_id)
            if cov.alarm:
                pct = round((cov.margin_fraction or 0) * 100)
                out.append(
                    Condition(
                        "coverage",
                        device_id,
                        f"{label}: falling behind the SD card",
                        f"Only {pct}% of the card's retention window is left before "
                        "clips that aren't archived yet get overwritten.",
                    )
                )

        # One gateway serves the whole fleet: checked once, keyed fleet-wide.
        provider = archiver.provider
        if rules.gateway_down and not gateway_checked and isinstance(provider, ReolinkCliProvider):
            gateway_checked = True
            if not provider.gateway.is_listening():
                out.append(
                    Condition(
                        "gateway_down",
                        None,
                        "Camera gateway unreachable",
                        f"The reolink-cli gateway has been down for at least "
                        f"{rules.gateway_down_minutes} minutes; no camera can be archived.",
                        grace=timedelta(minutes=rules.gateway_down_minutes),
                    )
                )
    return out


# -- the notifier ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendResult:
    channel: str
    ok: bool
    error: str | None = None


class Notifier:
    def __init__(
        self,
        *,
        settings: Settings | Callable[[], Settings],
        repository: Repository,
        fleet: Fleet,
        publish: Callable[..., None] | None = None,
        channels: list[Channel] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # A callable gives the live settings (the daemon's ConfigStore), so
        # rule and channel edits apply at the next evaluation.
        self._settings = settings if callable(settings) else (lambda: settings)
        self.repo = repository
        self.fleet = fleet
        self.publish = publish or (lambda *_a, **_k: None)
        self._fixed_channels = channels
        self.clock = clock

    @property
    def settings(self) -> Settings:
        return self._settings()

    @property
    def channels(self) -> list[Channel]:
        """Built from the live `[alerts]` table unless fixed (tests)."""
        if self._fixed_channels is not None:
            return self._fixed_channels
        return build_channels(self.settings.alerts)

    @channels.setter
    def channels(self, value: list[Channel]) -> None:
        self._fixed_channels = value

    def send(self, msg: Message) -> list[SendResult]:
        results = []
        for channel in self.channels:
            try:
                channel.send(msg)
                results.append(SendResult(channel.name, True))
            except Exception as exc:  # noqa: BLE001 - one channel failing must not stop the rest
                # Only the exception type and its own text: never the URL,
                # which can itself be the credential (Slack/Discord).
                error = f"{type(exc).__name__}: {exc}"
                logger.error("notify.send_failed", channel=channel.name, error=error)
                results.append(SendResult(channel.name, False, error))
        return results

    def evaluate(self) -> None:
        """Never raises: it runs as a scheduler job every minute."""
        try:
            self._evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.error("notify.evaluate_failed", error=str(exc), exc_info=exc)

    def _evaluate(self) -> None:
        rules = self.settings.alerts.rules
        now = self.clock()
        current = (
            {c.key: c for c in current_conditions(self.fleet, self.repo, rules)}
            if rules.enabled
            else {}
        )
        stored = {a.key: a for a in self.repo.list_alerts()}
        changed = False

        for key, cond in current.items():
            self.repo.open_alert(
                key=key,
                condition=cond.condition,
                device_id=cond.device_id,
                title=cond.title,
                message=cond.message,
                now=now,
            )
            row = stored.get(key)
            first_seen = parse_iso_utc(row.first_seen_at) if row else now
            if row is None or row.last_sent_at is None:
                due = now - first_seen >= cond.grace
            else:
                due = now - parse_iso_utc(row.last_sent_at) >= timedelta(hours=rules.renotify_hours)
            if row is None:
                changed = True
            sent = due and self.channels and self.send(Message(cond.title, cond.message))
            if sent and any(r.ok for r in sent):
                self.repo.mark_alert_sent(key, now)

        for key, row in stored.items():
            if key in current:
                continue
            self.repo.delete_alert(key)
            changed = True
            # Only resolve what someone was actually told about, and never
            # when the rules themselves were just switched off.
            if rules.enabled and rules.notify_on_resolve and row.last_sent_at and self.channels:
                self.send(Message(f"Resolved: {row.title}", row.message, True))

        if changed:
            self.publish("alert")


def channel_status(cfg: AlertsConfig) -> list[dict[str, Any]]:
    """For the dashboard: which channels are configured, nothing secret."""
    return [
        {"name": "ntfy", "configured": bool(cfg.ntfy_url)},
        {"name": "webhook", "configured": cfg.webhook_url_file is not None},
        {"name": "email", "configured": bool(cfg.smtp_host and cfg.smtp_to)},
    ]
