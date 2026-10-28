"""Shared web fixtures: a ReoVault app over a temp config with one demo
camera and a real restic repository location."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reovault.camera import Gateway
from reovault.config import Settings, load_settings
from reovault.config_store import ConfigStore
from reovault.db import Db
from reovault.runtime import Runtime
from reovault.web import security
from reovault.web.app import create_app
from reovault.web.auth import write_password_file

WEB_TEST_EMAIL = "test@example.com"
WEB_TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    pw = tmp_path / "restic_password"
    pw.write_text("test")
    toml = tmp_path / "reovault.toml"
    toml.write_text(
        f"""
[[cameras]]
alias = "demo"
timezone = "Europe/Brussels"
demo = true

[backup]
repository = "{tmp_path / "repos"}"
password_file = "{pw}"

[web]
email = "{WEB_TEST_EMAIL}"
cookie_secure = false
password_file = "{tmp_path / "web_password"}"
session_key_path = "{tmp_path / "session.key"}"

[storage]
data_dir = "{tmp_path / "data"}"
staging_dir = "{tmp_path / "staging"}"

[tools]
restic = "{shutil.which("restic") or "restic"}"
"""
    )
    return SimpleNamespace(tmp_path=tmp_path, toml=toml)


@pytest.fixture
def web_settings(env) -> Settings:
    return load_settings(env.toml)


@pytest.fixture
def web_device() -> str:
    return "demo"


@pytest.fixture
def web_password_hash(web_settings) -> str:
    phc = security.hash_password(WEB_TEST_PASSWORD.encode())
    write_password_file(web_settings.web.password_file, phc)
    return phc


def make_runtime(env, web_settings: Settings) -> Runtime:
    return Runtime(
        config=ConfigStore(env.toml, web_settings),
        db=Db(web_settings.storage.data_dir / "reovault.db"),
        gateway=Gateway("reolink-cli", "127.0.0.1:1"),
    )


def make_web_app(env, web_settings: Settings, web_device: str | None = None) -> FastAPI:
    return create_app(make_runtime(env, web_settings), dashboard_dir=env.tmp_path / "no-dist")


def csrf_token(client: TestClient) -> str:
    token: str = client.get("/api/v1/session").json()["csrf_token"]
    return token


def login(client: TestClient) -> TestClient:
    response = client.post(
        "/api/v1/login",
        json={"email": WEB_TEST_EMAIL, "password": WEB_TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf_token(client)},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.fixture
def client(env, web_settings, web_device, web_password_hash) -> TestClient:
    return TestClient(make_web_app(env, web_settings, web_device))
