"""Talking to cameras: the official `reolink-cli`, and nothing else.

Verified against a D340W (reolink-cli 0.19.0, 2026-10-28):
- search, storage status and download all need the reolink-gateway sidecar;
- a batch `vod download a b c --directory D` exits 0 even when a name can't be
  fetched, reporting `data.skipped[{file, reason}]` and `data.files[{file,
  bytes}]`, and the downloaded sizes equal `vod search`'s `fileSize`;
- the skip reason for a clip the card no longer has is not reliably the
  "not found" text, so callers decide "gone" by searching again.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from reovault.logging import get_logger
from reovault.tools import ToolError, describe_exit, run

logger = get_logger(__name__)

_LOCAL_ISO = "%Y-%m-%dT%H:%M:%S"
# Field names seen on the D340W first; the rest are tolerated fallbacks.
_NAME_KEYS = ("name", "fileName", "remoteName")
_START_KEYS = ("startTime", "start", "beginTime")
_END_KEYS = ("endTime", "end")
_SIZE_KEYS = ("fileSize", "size")
_TYPE_KEYS = ("recordType", "type", "recType")
# reolink-cli exit codes that mean "the camera or the network is the problem":
# the whole invocation failed, so no single clip is to blame.
_TRANSIENT_CODES = {2, 3}  # network, auth
_NETWORK_EXIT = 2
_SHORT_CALL_SECS = 60
_HARDWARE_SCOPE_RE = re.compile(r"^onvif://[^/]+/hardware/(?P<model>.+)$")
_NAME_SCOPE_RE = re.compile(r"^reolink-lan://name/(?P<name>.+)$")
# The card scan walks back one local day at a time and stops after this many
# empty days in a row; a camera that recorded nothing for longer makes the
# oldest clip look newer than it is (only ever shown, never acted on).
EMPTY_DAYS_TO_STOP = 3
_MAX_SCAN_DAYS = 3650


@dataclass(frozen=True, slots=True)
class Found:
    """One clip as the card lists it. Times are UTC."""

    name: str
    start_utc: datetime
    end_utc: datetime | None
    size: int | None
    rec_type: str | None


@dataclass(frozen=True, slots=True)
class CardStatus:
    total_gb: float | None
    free_gb: float | None
    mounted: bool | None


@dataclass(slots=True)
class Downloaded:
    files: dict[str, Path] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # name -> reason


class Camera(Protocol):
    id: str
    timezone: str

    def search(self, from_utc: datetime, to_utc: datetime) -> list[Found]: ...
    def download(self, names: list[str], dest: Path, timeout: float) -> Downloaded: ...
    def storage(self) -> CardStatus: ...


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((d[k] for k in keys if k in d), None)


def _target(host: str, user: str) -> list[str]:
    # The password goes over stdin, never argv (visible in `ps`).
    return ["--host", host, "--user", user, "--password-stdin"]


def _secret(password: SecretStr | None) -> str:
    return (password.get_secret_value() if password else "") + "\n"


def call(
    binary: str,
    args: list[str],
    *,
    timeout: float,
    gateway: str | None = None,
    stdin: str | None = None,
) -> Any:
    """`reolink-cli --output json <args>`; the parsed `data` on success."""
    cmd = [binary, *(["--gateway-addr", gateway] if gateway else []), "--output", "json", *args]
    try:
        proc = run(cmd, timeout=timeout, stdin=stdin)
    except ToolError as exc:
        # A short call (search, storage) that times out means nobody answered;
        # a long download timing out may just be slow.
        exc.unreachable = timeout <= _SHORT_CALL_SECS and exc.timed_out
        raise
    # Errors can come as the JSON envelope on stderr instead of stdout.
    out = proc.stdout.strip() or (proc.stderr.strip() if proc.returncode else "")
    try:
        envelope = json.loads(out) if out else {}
    except json.JSONDecodeError:
        if proc.returncode == 0:
            raise ToolError(
                "reolink-cli printed something that isn't JSON", detail=proc.stdout[:2000]
            ) from None
        envelope = {}
    if proc.returncode == 0 and envelope.get("ok", True):
        return envelope.get("data") or {}
    error = envelope.get("error") or {}
    message = (
        error.get("message") or proc.stderr.strip() or describe_exit("reolink-cli", proc.returncode)
    )
    raise ToolError(
        message,
        code=proc.returncode,
        detail=f"exit {proc.returncode}, code {error.get('code')}: {proc.stderr.strip()}",
        unreachable=proc.returncode == _NETWORK_EXIT or error.get("code") == "NETWORK_ERROR",
    )


class ReolinkCamera:
    """A camera reached by address, user and password from reovault.toml;
    reolink-cli keeps no registry of its own."""

    def __init__(
        self,
        *,
        id: str,
        timezone: str,
        host: str,
        user: str,
        password: SecretStr | None,
        binary: str,
        gateway: Gateway,
    ):
        self.id = id
        self.timezone = timezone
        self.tz = ZoneInfo(timezone)
        self.host, self.user, self.password = host, user, password
        self.binary = binary
        self.gateway = gateway

    def _call(self, args: list[str], timeout: float) -> Any:
        return call(
            self.binary,
            [*_target(self.host, self.user), *args],
            timeout=timeout,
            gateway=self.gateway.ensure(),
            stdin=_secret(self.password),
        )

    def search(self, from_utc: datetime, to_utc: datetime) -> list[Found]:
        """One `vod search` per camera-local day: search time grows with the
        clips on the card, and a multi-day search outruns the gateway's own
        60 s budget on a busy camera."""
        seen: dict[tuple[str, datetime], Found] = {}
        start = from_utc.astimezone(self.tz)
        end = to_utc.astimezone(self.tz)
        while start < end:
            midnight = datetime.combine(start.date() + timedelta(days=1), dtime(), tzinfo=self.tz)
            chunk_end = min(midnight, end)
            for clip in self._search_local(start, chunk_end):
                seen.setdefault((clip.name, clip.start_utc), clip)  # midnight-spanning clips
            start = chunk_end
        return list(seen.values())

    def _search_local(self, start: datetime, end: datetime) -> list[Found]:
        data = self._call(
            [
                "vod",
                "search",
                "--from",
                start.strftime(_LOCAL_ISO),
                "--to",
                end.strftime(_LOCAL_ISO),
                "--limit",
                "0",
            ],
            timeout=60,
        )
        if data.get("truncated"):
            logger.error("camera.search_truncated", camera=self.id, scanned=data.get("scanned"))
        items = next(
            (
                data[k]
                for k in ("files", "recordings", "items", "list")
                if isinstance(data.get(k), list)
            ),
            None,
        )
        if items is None:
            raise ToolError(
                "vod search answered without a recording list", detail=repr(data)[:2000]
            )
        return [self._parse(i) for i in items if isinstance(i, dict)]

    def _parse(self, item: dict[str, Any]) -> Found:
        name, start = _first(item, _NAME_KEYS), _first(item, _START_KEYS)
        if not isinstance(name, str) or not isinstance(start, str):
            raise ToolError("vod search returned a clip without a name or start", detail=repr(item))

        def utc(s: str) -> datetime:
            # Explicitly via the camera's zone: astimezone() alone would use
            # the container's zone.
            return datetime.strptime(s, _LOCAL_ISO).replace(tzinfo=self.tz).astimezone(UTC)

        end = _first(item, _END_KEYS)
        size = _first(item, _SIZE_KEYS)
        rec_type = _first(item, _TYPE_KEYS)
        return Found(
            name=name,
            start_utc=utc(start),
            end_utc=utc(end) if isinstance(end, str) else None,
            size=size if isinstance(size, int) else None,
            rec_type=rec_type if isinstance(rec_type, str) else None,
        )

    def download(self, names: list[str], dest: Path, timeout: float) -> Downloaded:
        """One batch invocation, one device connection. Raises `ToolError`
        when the invocation itself failed (camera or network down): then no
        single clip is to blame."""
        dest.mkdir(parents=True, exist_ok=True)
        out = Downloaded()
        try:
            data = self._call(
                ["vod", "download", *names, "--directory", str(dest)], timeout=timeout
            )
        except ToolError as exc:
            if len(names) != 1 or exc.code is None or exc.code in _TRANSIENT_CODES:
                raise
            out.failed[names[0]] = str(exc)  # a one-name batch fails the whole call
            return out
        skipped = {
            s.get("file"): str(s.get("reason", "skipped")) for s in data.get("skipped") or []
        }
        for name in names:
            # Files are written as <name>.<codec>; trust the disk, not just the JSON.
            hits = sorted(dest.glob(f"{name}.*"))
            if name in skipped:
                out.failed[name] = skipped[name]
            elif hits:
                out.files[name] = hits[0]
            else:
                out.failed[name] = "reolink-cli reported no file for it"
        return out

    def storage(self) -> CardStatus:
        data = self._call(["storage", "status"], timeout=60)
        items = data.get("items")
        disk = items[0] if isinstance(items, list) and items else data
        return CardStatus(
            total_gb=disk.get("totalGB"), free_gb=disk.get("remainGB"), mounted=disk.get("mounted")
        )


@dataclass
class DemoCamera:
    """A pretend camera (`demo = true`): its card holds one tiny clip every
    `every` for the last `window`, sliding with the wall clock so old clips
    roll off the old end like a real looping card."""

    id: str
    timezone: str
    every: timedelta = timedelta(minutes=15)
    window: timedelta = timedelta(days=7)
    clip_bytes: int = 4096
    now: Any = field(default=lambda: datetime.now(UTC))
    _TYPES = ("md", "md,people", "md,vehicle", "md,people,vehicle")

    def _slot(self, i: int) -> Found:
        start = datetime.fromtimestamp(i * self.every.total_seconds(), UTC)
        return Found(
            name=f"01{start:%Y%m%d%H%M%S}",
            start_utc=start,
            end_utc=start + timedelta(seconds=30),
            size=self.clip_bytes,
            rec_type=self._TYPES[i % len(self._TYPES)],
        )

    def _index(self, name: str) -> int:
        start = datetime.strptime(name[2:], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return round(start.timestamp() / self.every.total_seconds())

    def search(self, from_utc: datetime, to_utc: datetime) -> list[Found]:
        step = self.every.total_seconds()
        lo = -(-max(from_utc, self.now() - self.window).timestamp() // step)
        hi = min(to_utc, self.now()).timestamp() // step
        slots = (self._slot(int(i)) for i in range(int(lo), int(hi) + 1))
        return [s for s in slots if s.start_utc < to_utc]

    def download(self, names: list[str], dest: Path, timeout: float) -> Downloaded:
        dest.mkdir(parents=True, exist_ok=True)
        out = Downloaded()
        oldest = self.now() - self.window
        for name in names:
            if self._slot(self._index(name)).start_utc < oldest:  # rolled off meanwhile
                out.failed[name] = "device rejected command 14 (400)"
                continue
            path = dest / f"{name}.mp4"
            path.write_bytes(name.encode().ljust(self.clip_bytes, b"\0"))
            out.files[name] = path
        return out

    def storage(self) -> CardStatus:
        return CardStatus(total_gb=64.0, free_gb=1.5, mounted=True)


def scan_card(camera: Camera, now: datetime) -> tuple[list[Found], datetime | None]:
    """Everything on the card plus its oldest clip, walking back one local
    day at a time until `EMPTY_DAYS_TO_STOP` empty days in a row."""
    tz = ZoneInfo(camera.timezone)
    day_end = now.astimezone(tz) + timedelta(hours=2)  # a camera clock running ahead
    found: list[Found] = []
    empty = 0
    for _ in range(_MAX_SCAN_DAYS):
        day_start = datetime.combine(day_end.date(), dtime(), tzinfo=tz)
        if day_start >= day_end:
            day_start -= timedelta(days=1)
        clips = camera.search(day_start.astimezone(UTC), day_end.astimezone(UTC))
        found += clips
        if clips:
            empty = 0
        elif day_start <= now:  # the look-ahead into tomorrow is not an empty day
            empty += 1
        if empty >= EMPTY_DAYS_TO_STOP:
            break
        day_end = day_start
    oldest = min((c.start_utc for c in found), default=None)
    return found, oldest


class Gateway:
    """The local `reolink-gateway` that reolink-cli needs for camera calls.
    Started on its own address so it never shares state with another tool's
    gateway, and stopped only by the PID it was started with:
    `reolink-cli gateway stop` kills every gateway on the host."""

    def __init__(self, binary: str, addr: str) -> None:
        self.binary = binary
        self.addr = addr
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def listening(self) -> bool:
        try:
            data = call(self.binary, ["gateway", "status"], timeout=5, gateway=self.addr)
        except ToolError:
            return False
        return bool(data.get("listening"))

    def ensure(self) -> str:
        """The gateway's address, starting it first if needed."""
        with self._lock:
            if self.listening():
                return self.addr
            logger.info("gateway.starting", addr=self.addr)
            self._proc = subprocess.Popen(
                [self.binary, "gateway", "start", "--addr", self.addr],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.listening():
                    return self.addr
                if self._proc.poll() is not None:
                    raise ToolError(f"reolink-gateway exited early (code {self._proc.returncode})")
                time.sleep(0.3)
            raise ToolError("reolink-gateway did not start within 15 s")

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


# -- adding cameras ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Discovered:
    host: str
    uid: str | None
    model: str | None
    name: str | None
    added: bool  # a configured camera already has this address


def discover(binary: str, known_hosts: set[str], timeout_secs: int = 2) -> list[Discovered]:
    data = call(
        binary, ["discover", "--timeout-secs", str(timeout_secs)], timeout=timeout_secs + 30
    )
    out = []
    for entry in data.get("devices") or []:
        model = name = None
        for scope in entry.get("scopes") or []:
            if isinstance(scope, str):
                if (m := _HARDWARE_SCOPE_RE.match(scope)) and model is None:
                    model = m.group("model")
                if (m := _NAME_SCOPE_RE.match(scope)) and name is None:
                    name = m.group("name")
        host = entry.get("host") if isinstance(entry.get("host"), str) else ""
        out.append(Discovered(host, entry.get("uid"), model, name, host in known_hosts))
    return out


def check(binary: str, gateway: Gateway, *, host: str, user: str, password: SecretStr) -> None:
    """Can ReoVault log in and read the card? Raises `ToolError` with the
    camera's own reason (wrong password, unreachable) before anything is saved."""
    call(
        binary,
        [*_target(host, user), "storage", "status"],
        timeout=_SHORT_CALL_SECS,
        gateway=gateway.ensure(),
        stdin=_secret(password),
    )
