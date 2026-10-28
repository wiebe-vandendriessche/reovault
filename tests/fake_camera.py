"""A scriptable camera for engine tests, shaped by what the D340W actually
does (see reovault/camera.py): batch downloads report skipped names instead
of failing, an offline camera fails the whole invocation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from reovault.camera import CardStatus, Downloaded, Found
from reovault.tools import ToolError


@dataclass
class FakeCamera:
    alias: str = "cam"
    timezone: str = "Europe/Brussels"
    card: dict[str, tuple[Found, bytes]] = field(default_factory=dict)
    offline: bool = False
    short_once: set[str] = field(default_factory=set)  # first download comes up short
    always_short: set[str] = field(default_factory=set)
    refuse: set[str] = field(default_factory=set)  # skipped, but still on the card
    vanish: set[str] = field(default_factory=set)  # overwritten just before download
    downloads: list[list[str]] = field(default_factory=list)
    on_download: object = None  # callable(dest) to observe staging mid-run

    def add(self, start: datetime, size: int = 1000, rec_type: str = "md") -> str:
        name = f"01{start:%Y%m%d%H%M%S}"
        clip = Found(name, start, start + timedelta(seconds=30), size, rec_type)
        self.card[name] = (clip, os.urandom(size))
        return name

    def search(self, from_utc: datetime, to_utc: datetime) -> list[Found]:
        if self.offline:
            raise ToolError("camera unreachable", code=2)
        return [c for c, _ in self.card.values() if from_utc <= c.start_utc < to_utc]

    def download(self, names: list[str], dest: Path, timeout: float) -> Downloaded:
        if self.offline:
            raise ToolError("camera unreachable", code=2)
        self.downloads.append(list(names))
        if callable(self.on_download):
            self.on_download(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = Downloaded()
        for name in names:
            if name in self.vanish:
                self.card.pop(name, None)
            if name not in self.card or name in self.refuse:
                out.failed[name] = "device rejected command 14 (400)"
                continue
            data = self.card[name][1]
            if name in self.always_short or name in self.short_once:
                self.short_once.discard(name)
                data = data[: len(data) // 2]
            path = dest / f"{name}.mp4"
            path.write_bytes(data)
            out.files[name] = path
        return out

    def storage(self) -> CardStatus:
        return CardStatus(total_gb=64.0, free_gb=1.0, mounted=True)
