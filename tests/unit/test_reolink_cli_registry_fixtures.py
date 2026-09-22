"""Fixture-pinned parsing tests for `ReolinkCliRegistry` (see
`tests/fixtures/reolink_cli/README.md`). Runs the real parsing code against
real captured payloads, with `subprocess.run` mocked to return the
fixture's exact bytes, so a `reolink-cli` upgrade that changes these shapes
fails loudly here instead of silently drifting.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli_registry import ReolinkCliRegistry

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "reolink_cli"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _completed(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


@pytest.fixture
def registry():
    return ReolinkCliRegistry(binary="reolink-cli")


def test_discover_fixture_parses_model_and_name_from_scopes(registry):
    fixture = _load("discover.json")
    empty_list = _completed({"ok": True, "data": [], "error": None})
    with patch("subprocess.run", side_effect=[empty_list, _completed(fixture)]):
        discovered = registry.discover()

    assert len(discovered) == 1
    device = discovered[0]
    assert device.host == "192.0.2.10:9000"
    assert device.protocol == "v20"
    # Confirmed by the fixture README: no dedicated model/name field, both
    # come out of the scopes list.
    assert device.model == "D340W"
    assert device.name == "Front door"
    assert device.already_registered is False


def test_discover_marks_already_registered_devices(registry):
    discover_fixture = _load("discover.json")
    device_list_fixture = _load("device_list.json")
    # discover() calls list_devices() first to cross-reference hosts, then
    # discover itself: two subprocess calls, in that order.
    with patch(
        "subprocess.run",
        side_effect=[_completed(device_list_fixture), _completed(discover_fixture)],
    ):
        discovered = registry.discover()

    assert discovered[0].already_registered is True  # same host as device_list.json


def test_device_list_fixture_parses(registry):
    fixture = _load("device_list.json")
    with patch("subprocess.run", return_value=_completed(fixture)):
        devices = registry.list_devices()

    assert len(devices) == 1
    d = devices[0]
    assert d.alias == "doorbell"
    assert d.has_password is True
    assert d.channel is None  # confirmed real: not always an int
    assert d.user == "admin"


def test_device_info_fixture_parses(registry):
    gateway = MagicMock(spec=GatewaySupervisor)
    gateway.addr = "127.0.0.1:9000"
    registry.gateway = gateway
    fixture = _load("device_info.json")
    with patch("subprocess.run", return_value=_completed(fixture)):
        info = registry.device_info(alias="doorbell")

    assert info.model == "D340W"
    assert info.firmware == "v3.0.0.6460_2605271708"


def test_device_info_without_a_gateway_raises_local_error(registry):
    from reovault.providers.base import LocalError

    with pytest.raises(LocalError):
        registry.device_info(alias="doorbell")
