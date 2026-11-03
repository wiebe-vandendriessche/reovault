"""A throwaway, healthy ReoVault with two demo cameras, for the browser smoke
test, the README screenshots and trying the dashboard without hardware.

Everything lives in one temp folder: config, database, restic repository and
password. It never touches a real camera, reolink-cli's registry or a real
alert channel (alerts go to a local sink). After the first backups it checks
the Overview reports nothing wrong, since the screenshots must show a
healthy instance.

    python3 scripts/demo_instance.py      # runs until Ctrl-C, prints the URL
"""

from __future__ import annotations

import http.cookiejar
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = "demo@example.com"
PASSWORD = "reovault-demo"


@dataclass(frozen=True)
class Instance:
    url: str
    email: str
    password: str
    folder: Path

    def setup_code(self) -> str:
        """The one-time code a fresh instance printed in its log."""
        for line in (self.folder / "daemon.log").read_text().splitlines():
            if line.startswith("{") and '"setup.code"' in line:
                return str(json.loads(line)["code"])
        raise RuntimeError("no setup code in the daemon log")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Sink(http.server.BaseHTTPRequestHandler):
    """Accepts alert posts so the demo has an alert channel configured."""

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class Session:
    """Logged-in API calls, the way the dashboard makes them."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.csrf = ""

    def call(self, method: str, path: str, body: object = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/v1{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        with self.opener.open(req, timeout=30) as resp:
            out = json.load(resp)
        if isinstance(out, dict) and out.get("csrf_token"):
            self.csrf = out["csrf_token"]
        return out

    def login(self) -> Session:
        self.call("GET", "/session")
        self.call("POST", "/login", {"email": EMAIL, "password": PASSWORD})
        return self


def _wait(predicate: object, what: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():  # type: ignore[operator]
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"demo instance: {what} within {timeout:.0f} s")
        time.sleep(0.5)


@contextmanager
def demo_instance(
    restic: str = "restic", rclone: str = "rclone", *, fresh: bool = False
) -> Iterator[Instance]:
    sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="reovault-demo-") as tmp:
        folder = Path(tmp)
        port = _free_port()
        (folder / "restic_password").write_text("demo-only-password")
        # Looks like a real cloud destination on screen, stays in this folder.
        (folder / "rclone.conf").write_text(
            f"[nextcloud]\ntype = alias\nremote = {folder / 'remote'}\n"
        )
        os.chmod(folder / "restic_password", 0o600)
        config = folder / "reovault.toml"
        config.write_text(
            f"""
[[cameras]]
alias = "front-door"
name = "Front door"
timezone = "Europe/Brussels"
demo = true

[[cameras]]
alias = "driveway"
name = "Driveway"
timezone = "Europe/Brussels"
demo = true

[[destinations]]
name = "nextcloud"
repository = "rclone:nextcloud:reovault"
keep_days = 14

[backup]
password_file = "{folder / "restic_password"}"

[alerts]
ntfy_url = "http://127.0.0.1:{sink.server_address[1]}/reovault"

[web]
host = "127.0.0.1"
port = {port}
{"" if fresh else f'email = "{EMAIL}"'}
cookie_secure = false
password_file = "{folder / "web_password"}"
session_key_path = "{folder / "web_session.key"}"

[storage]
data_dir = "{folder / "data"}"
staging_dir = "{folder / "data" / "staging"}"

[tools]
restic = "{restic}"
rclone = "{rclone}"
gateway_addr = "127.0.0.1:{_free_port()}"
"""
        )
        sys.path.insert(0, str(ROOT))
        from reovault.web import security
        from reovault.web.auth import write_password_file

        if not fresh:  # a fresh instance starts without a login: the setup screen makes it
            write_password_file(folder / "web_password", security.hash_password(PASSWORD.encode()))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("REOVAULT_", "RESTIC_"))}
        env["REOVAULT_CONFIG_FILE"] = str(config)
        env["RCLONE_CONFIG"] = str(folder / "rclone.conf")
        # restic starts `rclone` from PATH.
        env["PATH"] = os.pathsep.join([str(Path(rclone).parent), env.get("PATH", "")])
        log = open(folder / "daemon.log", "w")  # noqa: SIM115 - closed below
        proc = subprocess.Popen(
            [sys.executable, "-m", "reovault", "daemon"], cwd=ROOT, env=env, stdout=log, stderr=log
        )
        url = f"http://127.0.0.1:{port}"
        try:
            _wait(
                lambda: urllib.request.urlopen(f"{url}/healthz", timeout=2).status == 200, "start"
            )
            if fresh:
                yield Instance(url, EMAIL, PASSWORD, folder)
                return
            api = Session(url).login()

            def backed_up() -> bool:
                cams = api.call("GET", "/overview")["cameras"]  # type: ignore[index]
                return all(c["verdict"] == "up_to_date" for c in cams)

            _wait(backed_up, "both demo cameras backed up")
            # Alerts are evaluated once a minute; healthy means none open.
            api.call("POST", "/recovery/confirm")  # the screenshots show no reminder
            issues = api.call("GET", "/overview")["issues"]  # type: ignore[index]
            if issues:
                raise RuntimeError(f"demo instance is not healthy: {issues}")
            yield Instance(url, EMAIL, PASSWORD, folder)
        except BaseException:
            log.flush()
            sys.stderr.write((folder / "daemon.log").read_text()[-4000:])
            raise
        finally:
            proc.terminate()
            proc.wait(timeout=15)
            log.close()
            sink.shutdown()


if __name__ == "__main__":
    tools = ROOT / ".tools"
    with demo_instance(
        shutil.which("restic") or str(tools / "restic"),
        shutil.which("rclone") or str(tools / "rclone"),
    ) as demo:
        print(f"{demo.url}  login {demo.email} / {demo.password}  (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
