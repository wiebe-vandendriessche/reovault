"""`CameraRegistry` backed by the real `reolink-cli` subprocess.

- `discover --output json` (no gateway needed) ->
  `data.devices[].{host, uid, mac, protocol, scopes[]}`. Model comes from
  an `onvif://...hardware/<MODEL>` scope entry; the camera's friendly name
  from a `reolink-lan://name/<NAME>` entry. Payload shape (host/uid/mac
  redacted to placeholders below, everything else verbatim):
  `{"ok":true,...,"data":{"devices":[{"host":"192.0.2.10:9000",
  "uid":"REDACTED","mac":"AA:BB:CC:DD:EE:FF","protocol":"v20",
  "scopes":["onvif://www.onvif.org/hardware/D340W",
  "reolink-lan://name/Front door"]}]}}`.
- `device list --output json` (no gateway needed) ->
  `data[].{camera, host, user, hasPassword, channel, description,
  disabled}`. `hasPassword` is a boolean; the password itself never
  appears. `channel` can be `null` (an unset/single-channel device), not
  just an int.
- `device add <alias> --host H --user U --password-stdin --output json`
  (no gateway needed) -> `data.action == "added"`. The password lands in
  `aliases.toml` AES-GCM-encrypted (`RLENC1:` prefix), file mode 0600, via
  reolink-cli's own default credentials.key resolution; nothing here
  manages that key.
- `info --camera <alias> --gateway-addr ... --output json` (gateway
  required) -> `data.{model, firmware, name, ...}`. Payload:
  `{"ok":true,...,"data":{"model":"D340W",
  "firmware":"v3.0.0.6460_2605271708","name":"Front door",...}}`.

Deliberately does NOT pass `--config-file`/`--cameras-file`: the existing
`ReolinkCliProvider` already relies on reolink-cli's own default aliases
location, so a camera this registry adds must land in that same file, or
the provider that's supposed to archive it would never find it. Isolating
ReoVault onto its own reolink-cli config directory is a deployment-time
(container HOME/mount) decision, not something this module needs to know
about.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import SecretStr

from reovault.logging import get_logger
from reovault.models import DeviceInfo, DiscoveredDevice, RegisteredDevice
from reovault.providers._cli import run_cli as _run_cli
from reovault.providers.base import CameraRegistry, LocalError
from reovault.providers.gateway import GatewaySupervisor

logger = get_logger(__name__)

_HARDWARE_SCOPE_RE = re.compile(r"^onvif://[^/]+/hardware/(?P<model>.+)$")
_NAME_SCOPE_RE = re.compile(r"^reolink-lan://name/(?P<name>.+)$")


def _parse_discovered(entry: dict[str, Any], registered_hosts: frozenset[str]) -> DiscoveredDevice:
    scopes = entry.get("scopes") or []
    model = None
    name = None
    for scope in scopes:
        if not isinstance(scope, str):
            continue
        if (m := _HARDWARE_SCOPE_RE.match(scope)) and model is None:
            model = m.group("model")
        if (m := _NAME_SCOPE_RE.match(scope)) and name is None:
            name = m.group("name")
    host = entry.get("host")
    return DiscoveredDevice(
        host=host if isinstance(host, str) else "",
        uid=entry.get("uid"),
        mac=entry.get("mac"),
        protocol=entry.get("protocol"),
        model=model,
        name=name,
        already_registered=host in registered_hosts,
    )


def _parse_registered(entry: dict[str, Any]) -> RegisteredDevice:
    return RegisteredDevice(
        alias=str(entry.get("camera", "")),
        host=entry.get("host"),
        user=entry.get("user"),
        has_password=bool(entry.get("hasPassword", False)),
        channel=entry.get("channel"),
        description=entry.get("description"),
        disabled=bool(entry.get("disabled", False)),
    )


class ReolinkCliRegistry(CameraRegistry):
    def __init__(
        self, *, binary: str, gateway: GatewaySupervisor | None = None, timeout_secs: int = 30
    ) -> None:
        self.binary = binary
        # Only `device_info` needs this; every other method here works
        # without a gateway. Optional so a registry built purely for
        # discovery/add never has to wire one up.
        self.gateway = gateway
        self.timeout_secs = timeout_secs

    def discover(self, *, timeout_secs: int = 2) -> list[DiscoveredDevice]:
        registered = {d.host for d in self.list_devices() if d.host}
        envelope = _run_cli(
            self.binary,
            ["discover", "--timeout-secs", str(timeout_secs)],
            gateway=None,
            timeout=timeout_secs + self.timeout_secs,
        )
        devices = (envelope.get("data") or {}).get("devices") or []
        return [_parse_discovered(d, frozenset(registered)) for d in devices if isinstance(d, dict)]

    def list_devices(self) -> list[RegisteredDevice]:
        envelope = _run_cli(
            self.binary, ["device", "list"], gateway=None, timeout=self.timeout_secs
        )
        rows = envelope.get("data") or []
        return [_parse_registered(r) for r in rows if isinstance(r, dict)]

    def add_device(
        self,
        *,
        alias: str,
        host: str,
        user: str,
        password: SecretStr,
        channel: int | None = None,
    ) -> None:
        args = ["device", "add", alias, "--host", host, "--user", user, "--password-stdin"]
        if channel is not None:
            args += ["--channel", str(channel)]
        # One line, newline-terminated: matches `device add --help`'s own
        # description ("one line, no echo"). The secret's value is read
        # once via get_secret_value() and never bound to a local that could
        # linger in a traceback frame.
        _run_cli(
            self.binary,
            args,
            stdin_line=password.get_secret_value() + "\n",
            timeout=self.timeout_secs,
        )

    def set_password(self, *, alias: str, password: SecretStr) -> None:
        _run_cli(
            self.binary,
            ["device", "update", alias, "--password-stdin"],
            stdin_line=password.get_secret_value() + "\n",
            timeout=self.timeout_secs,
        )

    def device_info(self, *, alias: str) -> DeviceInfo:
        if self.gateway is None:
            raise LocalError("device_info needs a GatewaySupervisor; none was configured")
        envelope = _run_cli(
            self.binary,
            ["info", "--camera", alias],
            gateway=self.gateway,
            timeout=self.timeout_secs,
        )
        data = envelope.get("data") or {}
        return DeviceInfo(
            model=data.get("model"),
            firmware=data.get("firmware"),
            name=data.get("name"),
            serial=data.get("serial"),
        )
