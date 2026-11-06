"""Creating rclone remotes through rclone itself, with the real binary."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reovault import rclone
from reovault.tools import ToolError

TOKEN = (
    '{"access_token":"x","token_type":"Bearer","refresh_token":"y","expiry":"2030-01-01T00:00:00Z"}'
)


@pytest.fixture
def binary(tmp_path, monkeypatch):
    path = shutil.which("rclone")
    if path is None:
        pytest.fail("rclone is not on PATH (python3 scripts/fetch_tools.py .tools)")
    conf = tmp_path / "rclone.conf"
    conf.write_text("# my own remotes\n[mine]\ntype = local\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))  # as in the container
    return path, str(conf)


def test_a_webdav_remote_is_created_with_the_password_obscured(binary):
    path, conf = binary
    name = rclone.create(
        path, "cloud", rclone.BY_ID["nextcloud"],
        {"url": "https://x.example/dav/", "user": "me", "pass": "hunter2-secret"},
    )  # fmt: skip
    text = Path(conf).read_text()
    assert name == "reovault-cloud" and "[reovault-cloud]" in text
    assert "hunter2-secret" not in text  # rclone obscured it
    assert "vendor = nextcloud" in text  # the default
    assert "[mine]" in text and "# my own remotes" in text  # hand-made remotes untouched
    assert "reovault-cloud" in rclone.remotes(path)


def test_missing_fields_are_named(binary):
    path, conf = binary
    with pytest.raises(ToolError, match="Username"):
        rclone.create(path, "cloud", rclone.BY_ID["nextcloud"], {"url": "https://x/", "pass": "p"})


def test_an_existing_remote_is_never_overwritten(binary):
    path, conf = binary
    values = {"url": "https://x/", "user": "u", "pass": "p"}
    rclone.create(path, "cloud", rclone.BY_ID["nextcloud"], values)
    with pytest.raises(ToolError, match="already exists"):
        rclone.create(path, "cloud", rclone.BY_ID["nextcloud"], values)


def test_a_pasted_onedrive_section_becomes_a_remote(binary):
    path, conf = binary
    section = f"[work]\ntype = onedrive\ntoken = {TOKEN}\ndrive_id = ABC\ndrive_type = business\n"
    name = rclone.create(path, "onedrive", rclone.BY_ID["onedrive"], {"section": section})
    text = Path(conf).read_text()
    assert name == "reovault-onedrive" and "drive_id = ABC" in text


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("[a]\ntype = local\ntoken = x\n", "type must be onedrive"),
        ("[a]\ntype = onedrive\n", "no token"),
        ("[a]\ntype = onedrive\ntoken = x\ncommand = rm -rf /\n", "aren't accepted"),
        ("[a]\ntype = onedrive\ntoken = x\n[b]\ntype = onedrive\ntoken = y\n", "exactly one"),
    ],
)
def test_a_pasted_section_is_checked(binary, section, message):
    path, conf = binary
    with pytest.raises(ToolError, match=message):
        rclone.create(path, "od", rclone.BY_ID["onedrive"], {"section": section})
    assert "reovault-od" not in rclone.remotes(path)


def test_an_unreachable_remote_fails_with_a_readable_reason_and_no_secret(binary):
    path, conf = binary
    name = rclone.create(
        path, "dead", rclone.BY_ID["nextcloud"],
        {"url": "http://127.0.0.1:9/", "user": "u", "pass": "topsecretpass", "vendor": "other"},
    )  # fmt: skip
    with pytest.raises(ToolError) as err:
        rclone.check(path, name)
    assert "topsecretpass" not in str(err.value)
    rclone.delete(path, name)
    assert name not in rclone.remotes(path)
