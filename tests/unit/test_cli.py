"""CLI tests for `doctor` and `key init|verify|backup`."""

from typer.testing import CliRunner

from reovault.cli import app

runner = CliRunner()


def _toml(tmp_path) -> str:
    config = tmp_path / "reovault.toml"
    config.write_text(
        f"""
        [storage]
        config_dir = "{tmp_path / "config"}"
        vault_dir = "{tmp_path / "vault"}"
        staging_dir = "{tmp_path / "staging"}"
        db_path = "{tmp_path / "config" / "reovault.db"}"
        master_key_path = "{tmp_path / "config" / "master.key"}"

        [reolink_cli]
        registry_dir = "{tmp_path / "reolink-cli"}"
        """
    )
    return str(config)


def test_doctor_warns_when_no_key_yet(tmp_path):
    result = runner.invoke(app, ["doctor"], env={"REOVAULT_CONFIG_FILE": _toml(tmp_path)})
    assert result.exit_code == 0
    assert "no master key yet" in result.stdout


def test_key_init_then_doctor_ok_with_passphrase(tmp_path):
    config_file = _toml(tmp_path)
    env = {"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "correct horse"}

    init_result = runner.invoke(app, ["key", "init"], env=env)
    assert init_result.exit_code == 0, init_result.output
    assert "Master key created" in init_result.output

    doctor_result = runner.invoke(app, ["doctor"], env=env)
    assert doctor_result.exit_code == 0
    assert "master key unwraps with the configured passphrase" in doctor_result.stdout


def test_doctor_fails_on_wrong_passphrase(tmp_path):
    config_file = _toml(tmp_path)
    runner.invoke(
        app,
        ["key", "init"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "right"},
    )
    result = runner.invoke(
        app,
        ["doctor"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "wrong"},
    )
    assert result.exit_code == 1
    assert "does not unwrap" in result.stdout


def test_key_init_refuses_to_overwrite(tmp_path):
    config_file = _toml(tmp_path)
    env = {"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "first"}
    runner.invoke(app, ["key", "init"], env=env)
    result = runner.invoke(app, ["key", "init"], env=env)
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_key_verify_true_and_false(tmp_path):
    config_file = _toml(tmp_path)
    runner.invoke(
        app,
        ["key", "init"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "right"},
    )

    ok = runner.invoke(
        app,
        ["key", "verify"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "right"},
    )
    assert ok.exit_code == 0
    assert "OK" in ok.output

    bad = runner.invoke(
        app,
        ["key", "verify"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "wrong"},
    )
    assert bad.exit_code == 1
    assert "FAIL" in bad.output


def test_key_verify_without_existing_key_fails(tmp_path):
    config_file = _toml(tmp_path)
    result = runner.invoke(
        app,
        ["key", "verify"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "x"},
    )
    assert result.exit_code == 1
    assert "no master key" in result.output


def test_key_backup_copies_file(tmp_path):
    config_file = _toml(tmp_path)
    runner.invoke(
        app,
        ["key", "init"],
        env={"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "right"},
    )
    dest = tmp_path / "backup.key"
    result = runner.invoke(
        app,
        ["key", "backup", "--to", str(dest)],
        env={"REOVAULT_CONFIG_FILE": config_file},
    )
    assert result.exit_code == 0
    assert dest.exists()
    assert (dest.stat().st_mode & 0o777) == 0o600
    assert "unrecoverable" in result.output


def test_doctor_fails_when_registry_dir_not_writable(tmp_path):
    config = tmp_path / "reovault.toml"
    registry_dir = tmp_path / "ro" / "reolink-cli"
    registry_dir.parent.mkdir(parents=True)
    registry_dir.parent.chmod(0o500)
    config.write_text(
        f"""
        [storage]
        config_dir = "{tmp_path / "config"}"
        vault_dir = "{tmp_path / "vault"}"
        staging_dir = "{tmp_path / "staging"}"
        db_path = "{tmp_path / "config" / "reovault.db"}"
        master_key_path = "{tmp_path / "config" / "master.key"}"

        [reolink_cli]
        registry_dir = "{registry_dir}"
        """
    )
    try:
        result = runner.invoke(app, ["doctor"], env={"REOVAULT_CONFIG_FILE": str(config)})
    finally:
        registry_dir.parent.chmod(0o700)
    assert result.exit_code == 1
    assert "registry_dir not writable" in result.output


def test_key_init_accepts_passphrase_from_file(tmp_path):
    config_file = _toml(tmp_path)
    passphrase_file = tmp_path / "passphrase.txt"
    passphrase_file.write_text("from a file\n")
    result = runner.invoke(
        app,
        ["key", "init"],
        env={
            "REOVAULT_CONFIG_FILE": config_file,
            "REOVAULT_MASTER_PASSPHRASE_FILE": str(passphrase_file),
        },
    )
    assert result.exit_code == 0

    doctor_result = runner.invoke(
        app,
        ["doctor"],
        env={
            "REOVAULT_CONFIG_FILE": config_file,
            "REOVAULT_MASTER_PASSPHRASE_FILE": str(passphrase_file),
        },
    )
    assert doctor_result.exit_code == 0
    assert "master key unwraps with the configured passphrase" in doctor_result.stdout
