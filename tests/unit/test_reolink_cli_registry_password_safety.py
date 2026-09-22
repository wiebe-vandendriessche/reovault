"""The credential invariant `ReolinkCliRegistry` exists to protect: never
persisted, never logged, never echoed, and never placed on a command line.
"""

import contextlib
import json
import subprocess
from unittest.mock import patch

import structlog
from pydantic import SecretStr

from reovault.providers.base import DeviceError
from reovault.providers.reolink_cli_registry import ReolinkCliRegistry

SECRET = "hunter2-super-secret-camera-password"  # noqa: S105 - test fixture, not real


def _completed(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


def test_add_device_never_puts_the_password_on_argv():
    registry = ReolinkCliRegistry(binary="reolink-cli")
    ok = _completed({"ok": True, "data": {"action": "added"}, "error": None})
    with patch("subprocess.run", return_value=ok) as spy:
        registry.add_device(
            alias="test-cam", host="192.168.1.50", user="admin", password=SecretStr(SECRET)
        )

    assert spy.call_count == 1
    call = spy.call_args
    argv = call.args[0]
    assert SECRET not in argv
    assert all(SECRET not in str(a) for a in argv)
    # It DOES go on stdin -- that's the sanctioned channel, not a leak.
    assert call.kwargs.get("input") == SECRET + "\n"


def test_set_password_never_puts_the_password_on_argv():
    registry = ReolinkCliRegistry(binary="reolink-cli")
    ok = _completed({"ok": True, "data": {}, "error": None})
    with patch("subprocess.run", return_value=ok) as spy:
        registry.set_password(alias="test-cam", password=SecretStr(SECRET))

    argv = spy.call_args.args[0]
    assert SECRET not in argv
    assert spy.call_args.kwargs.get("input") == SECRET + "\n"


def test_add_device_password_never_appears_in_any_log_record():
    registry = ReolinkCliRegistry(binary="reolink-cli")
    ok = _completed({"ok": True, "data": {"action": "added"}, "error": None})
    with (
        patch("subprocess.run", return_value=ok),
        structlog.testing.capture_logs() as logs,
    ):
        registry.add_device(
            alias="test-cam", host="192.168.1.50", user="admin", password=SecretStr(SECRET)
        )

    rendered = json.dumps(logs, default=str)
    assert SECRET not in rendered


def test_add_device_password_never_appears_in_logs_even_on_failure():
    """The failure path matters at least as much: a common instinct is to
    log the full request for debugging when something goes wrong."""
    registry = ReolinkCliRegistry(binary="reolink-cli")
    failed = subprocess.CompletedProcess(
        args=[],
        returncode=4,
        stdout=json.dumps({"ok": False, "error": {"code": "DEVICE_ERROR", "message": "no reply"}}),
        stderr="",
    )
    with (
        patch("subprocess.run", return_value=failed),
        structlog.testing.capture_logs() as logs,
        contextlib.suppress(DeviceError),
    ):
        registry.add_device(
            alias="test-cam", host="192.168.1.50", user="admin", password=SecretStr(SECRET)
        )

    rendered = json.dumps(logs, default=str)
    assert SECRET not in rendered


def test_secret_str_repr_never_shows_the_plaintext_password():
    """The property `SecretStr` is chosen specifically to guarantee: any
    accidental repr() (including inside a structlog kwarg) renders
    "**********", never the real value."""
    secret = SecretStr(SECRET)
    assert SECRET not in repr(secret)
    assert SECRET not in str(secret)
