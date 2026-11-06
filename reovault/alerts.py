"""Alerts: tell someone when footage isn't getting backed up.

State-based: `Notifier.evaluate()` runs once a minute, computes what is wrong
right now and diffs it against `alert_state`. A new condition opens an alert,
one that went away resolves it, one still open re-notifies every
`renotify_hours`. A restart neither loses nor repeats an alert.

Channels are stdlib only (ntfy and a JSON webhook over urllib, email over
smtplib). A failed send never raises; the alert stays unsent and is retried
on the next evaluation.
"""

from __future__ import annotations

import json
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

from reovault.config import AlertsConfig, Settings
from reovault.db import Db, parse_iso
from reovault.logging import get_logger

logger = get_logger(__name__)

_TIMEOUT_SECS = 10


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


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    # urllib also opens file:// and ftp://; a channel URL must be http(s).
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("alert URLs must be http:// or https://")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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
    """Generic JSON POST; `text` and `content` make Slack and Discord
    incoming-webhook URLs work as-is."""

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


def channel_status(cfg: AlertsConfig) -> list[dict[str, Any]]:
    """For the dashboard: which channels are configured, nothing secret."""
    return [
        {"name": "ntfy", "configured": bool(cfg.ntfy_url)},
        {"name": "webhook", "configured": cfg.webhook_url_file is not None},
        {"name": "email", "configured": bool(cfg.smtp_host and cfg.smtp_to)},
    ]


@dataclass(frozen=True, slots=True)
class Condition:
    condition: str
    camera: str | None
    title: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.condition}:{self.camera or '*'}"


@dataclass(frozen=True, slots=True)
class SendResult:
    channel: str
    ok: bool
    error: str | None = None


class Notifier:
    def __init__(
        self,
        *,
        settings: Callable[[], Settings],
        db: Db,
        conditions: Callable[[], list[Condition]],
        publish: Callable[..., None] = lambda *_a, **_k: None,
        channels: list[Channel] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.db, self.conditions = settings, db, conditions
        self.publish, self.clock = publish, clock
        self._fixed_channels = channels

    @property
    def channels(self) -> list[Channel]:
        if self._fixed_channels is not None:
            return self._fixed_channels
        return build_channels(self.settings().alerts)

    def send(self, msg: Message) -> list[SendResult]:
        results = []
        for channel in self.channels:
            try:
                channel.send(msg)
                results.append(SendResult(channel.name, True))
            except Exception as exc:  # noqa: BLE001 - one channel failing must not stop the rest
                # Only the exception's own text, never the URL (it can be the credential).
                error = f"{type(exc).__name__}: {exc}"
                logger.error("alerts.send_failed", channel=channel.name, error=error)
                results.append(SendResult(channel.name, False, error))
        return results

    def evaluate(self) -> None:
        """Never raises: it runs every minute."""
        try:
            self._evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.error("alerts.evaluate_failed", error=str(exc), exc_info=exc)

    def _evaluate(self) -> None:
        rules = self.settings().alerts.rules
        now = self.clock()
        current = {c.key: c for c in self.conditions()} if rules.enabled else {}
        stored = {r["key"]: r for r in self.db.list_alerts()}
        changed = False
        for key, cond in current.items():
            self.db.open_alert(
                key=key,
                condition=cond.condition,
                camera=cond.camera,
                title=cond.title,
                message=cond.message,
                now=now,
            )
            row = stored.get(key)
            changed |= row is None
            due = (
                row is None
                or row["last_sent_at"] is None
                or now - parse_iso(row["last_sent_at"]) >= timedelta(hours=rules.renotify_hours)
            )
            sent = due and self.channels and self.send(Message(cond.title, cond.message))
            if sent and any(r.ok for r in sent):
                self.db.mark_alert_sent(key, now)
        for key, row in stored.items():
            if key in current:
                continue
            self.db.delete_alert(key)
            changed = True
            # Only resolve what someone was told about, and not because the
            # rules themselves were just switched off.
            if rules.enabled and rules.notify_on_resolve and row["last_sent_at"] and self.channels:
                self.send(Message(f"Resolved: {row['title']}", row["message"], True))
        if changed:
            self.publish("alert")
