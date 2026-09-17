import subprocess
from unittest.mock import MagicMock, patch

import pytest

from reovault.providers.base import LocalError
from reovault.providers.gateway import GatewaySupervisor


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_is_listening_true_when_marker_present():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", return_value=_completed("[LISTENING] 127.0.0.1:9000\n")):
        assert gw.is_listening() is True


def test_is_listening_false_when_down():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch(
        "subprocess.run", return_value=_completed("[DOWN] run: reolink-cli gateway start\n")
    ):
        assert gw.is_listening() is False


def test_is_listening_false_on_missing_binary():
    gw = GatewaySupervisor(binary="reolink-cli")
    with patch("subprocess.run", side_effect=FileNotFoundError):
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
