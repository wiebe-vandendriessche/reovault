"""Unit tests for the small standalone helpers in archiver.py that don't
need a full Archiver/env fixture."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from reovault.archiver import (
    _parse_vault_path,
    assert_pinned_cli_version,
    assert_same_filesystem,
)


def test_assert_same_filesystem_passes_for_the_same_directory_tree(tmp_path):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault"
    assert_same_filesystem(staging, vault)  # both under tmp_path: same device


def test_assert_same_filesystem_raises_on_different_devices(tmp_path):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault"
    with patch("os.stat") as mock_stat:
        mock_stat.side_effect = [type("S", (), {"st_dev": 1})(), type("S", (), {"st_dev": 2})()]
        with pytest.raises(RuntimeError, match="different"):
            assert_same_filesystem(staging, vault)


def test_assert_pinned_cli_version_passes(tmp_path):
    with patch(
        "subprocess.run",
        return_value=type("R", (), {"stdout": "reolink-cli 0.19.0 (external)\n"})(),
    ):
        assert_pinned_cli_version("reolink-cli", "0.19.0")


def test_assert_pinned_cli_version_raises_on_mismatch():
    with (
        patch("subprocess.run", return_value=type("R", (), {"stdout": "reolink-cli 0.18.0\n"})()),
        pytest.raises(RuntimeError, match="0.19.0"),
    ):
        assert_pinned_cli_version("reolink-cli", "0.19.0")


def test_assert_pinned_cli_version_raises_when_binary_missing():
    with (
        patch("subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(RuntimeError, match="not found on PATH"),
    ):
        assert_pinned_cli_version("reolink-cli", "0.19.0")


def test_parse_vault_path_recovers_natural_key():
    parsed = _parse_vault_path("doorbell/2026/04/17/20260417T120000Z_clip1.mp4.enc")
    assert parsed is not None
    device_alias, start_utc, remote_name = parsed
    assert device_alias == "doorbell"
    assert start_utc == datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    assert remote_name == "clip1.mp4"


@pytest.mark.parametrize(
    "path",
    [
        "just-a-filename.enc",  # no directory component at all
        "doorbell/not-shaped-like-a-recording.enc",
        "doorbell/2026/04/17/no-underscore-separator.enc",
    ],
)
def test_parse_vault_path_returns_none_for_unrecognized_shapes(path):
    assert _parse_vault_path(path) is None
