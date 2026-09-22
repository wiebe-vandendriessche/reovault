from pathlib import Path

from reovault.config import load_settings


def test_defaults_with_no_toml_file(tmp_path):
    settings = load_settings(config_file=tmp_path / "does-not-exist.toml")
    assert settings.devices == []
    assert settings.reolink_cli.pinned_version == "0.19.0"
    assert settings.reolink_cli.gateway_addr == "127.0.0.1:9000"
    assert settings.web.port == 8080


def test_toml_file_is_read(tmp_path):
    toml_path = tmp_path / "reovault.toml"
    toml_path.write_text(
        """
        [[devices]]
        alias = "doorbell"
        channel = 0
        timezone = "Europe/Brussels"

        [web]
        port = 9090
        """
    )
    settings = load_settings(config_file=toml_path)
    assert len(settings.devices) == 1
    assert settings.devices[0].alias == "doorbell"
    assert settings.devices[0].timezone == "Europe/Brussels"
    assert settings.web.port == 9090


def test_env_var_overrides_toml_file(tmp_path, monkeypatch):
    toml_path = tmp_path / "reovault.toml"
    toml_path.write_text("[web]\nport = 9090\n")
    monkeypatch.setenv("REOVAULT_WEB__PORT", "7000")
    settings = load_settings(config_file=toml_path)
    assert settings.web.port == 7000


def test_storage_paths_are_paths(tmp_path):
    settings = load_settings(config_file=tmp_path / "missing.toml")
    assert isinstance(settings.storage.vault_dir, Path)


def test_config_file_env_var_used_when_not_overridden(tmp_path, monkeypatch):
    toml_path = tmp_path / "custom.toml"
    toml_path.write_text("[web]\nport = 1234\n")
    monkeypatch.setenv("REOVAULT_CONFIG_FILE", str(toml_path))
    settings = load_settings()
    assert settings.web.port == 1234
