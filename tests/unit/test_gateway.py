import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from reovault.providers.base import LocalError
from reovault.providers.gateway import GatewaySupervisor


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _status_json(listening: bool) -> str:
    """Real shape, verified 2026-09-17 against the live binary (JSON is the
    default output; `[LISTENING]`/`[DOWN]` bracketed text is `--output text`
    specifically, not what a bare `gateway status` call returns)."""
    return json.dumps(
        {
            "ok": True,
            "command": "gateway.status",
            "protocol": None,
            "data": {"addr": "127.0.0.1:9000", "listening": listening},
            "error": None,
        }
    )


def test_is_listening_true_when_listening():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", return_value=_completed(_status_json(True))):
        assert gw.is_listening() is True


def test_is_listening_false_when_down():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", return_value=_completed(_status_json(False))):
        assert gw.is_listening() is False


def test_is_listening_false_on_missing_binary():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert gw.is_listening() is False


def test_is_listening_false_on_non_json_output():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", return_value=_completed("not json")):
        assert gw.is_listening() is False


def test_ensure_running_is_noop_when_already_listening():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch.object(gw, "is_listening", return_value=True), patch("subprocess.Popen") as popen:
        gw.ensure_running()
        popen.assert_not_called()


def test_ensure_running_starts_gateway_when_down():
    gw = GatewaySupervisor(binary="reolink-cli", poll_interval_secs=0.01)
    fake_process = MagicMock()
    fake_process.poll.return_value = None
    # Down on the pre-check, up on the first poll after spawning.
    listening_sequence = iter([False, True])
    with (
        patch.object(gw, "is_listening", side_effect=lambda: next(listening_sequence)),
        patch("subprocess.Popen", return_value=fake_process) as popen,
    ):
        gw.ensure_running()
        popen.assert_called_once()


def test_ensure_running_raises_if_process_exits_early():
    gw = GatewaySupervisor(binary="reolink-cli", poll_interval_secs=0.01, start_timeout_secs=1)
    fake_process = MagicMock()
    fake_process.poll.return_value = 1
    fake_process.returncode = 1
    with (
        patch.object(gw, "is_listening", return_value=False),
        patch("subprocess.Popen", return_value=fake_process),
        pytest.raises(LocalError),
    ):
        gw.ensure_running()


def test_ensure_running_raises_on_timeout():
    gw = GatewaySupervisor(binary="reolink-cli", poll_interval_secs=0.01, start_timeout_secs=0.05)
    fake_process = MagicMock()
    fake_process.poll.return_value = None
    with (
        patch.object(gw, "is_listening", return_value=False),
        patch("subprocess.Popen", return_value=fake_process),
        pytest.raises(LocalError),
    ):
        gw.ensure_running()


def test_stop_terminates_owned_process():
    gw = GatewaySupervisor(binary="reolink-cli")
    fake_process = MagicMock()
    fake_process.poll.return_value = None
    gw._process = fake_process
    gw.stop()
    fake_process.terminate.assert_called_once()
