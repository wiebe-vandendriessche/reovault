"""Which snapshots to forget: `keep_days` and the size quota, per destination.

ReoVault chooses the snapshot ids itself (restic's `--keep-within` can't know
about copies), under three rules:
- a camera's newest snapshot is never forgotten: it carries the index;
- on the primary, a snapshot is released only when every copy destination that
  should hold it already does, so a clip is never left with no other copy;
- a copy only takes snapshots its own `keep_days` and floor still want, or it
  would copy what its retention forgets the next day, forever.

Pure functions over small records, so every rule has a plain unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

GiB = 1024**3
QUOTA_HIGH = 0.90  # start cutting above this share of the cap ...
QUOTA_LOW = 0.85  # ... until back under this one (covers the index, the config
# repository and prune's unused space)


@dataclass(frozen=True, slots=True)
class Snap:
    """One snapshot on one destination."""

    camera: str
    id: str  # its own id in that repository
    root: str  # the id it had where it was first made
    time: datetime
    bytes: int = 0  # the footage it holds (sum of clip sizes)


@dataclass(frozen=True, slots=True)
class CopyView:
    """What a primary needs to know about a copy destination."""

    name: str
    keep_days: int
    floor: datetime | None  # never copy snapshots older than this
    roots: dict[str, frozenset[str]] = field(default_factory=dict)  # camera -> roots held


@dataclass(frozen=True, slots=True)
class Cut:
    ids: list[str]  # to forget now (own ids in the repository being pruned)
    short: bool = False  # the quota forced a cut into snapshots still inside keep_days
    too_small: bool = False  # still over the cap with only the newest snapshots left
    blocked: int = 0  # releasable by policy but a copy doesn't have them yet
    floor: datetime | None = None  # the newest snapshot the quota forgot: never copy older


def newest_by_camera(snaps: list[Snap]) -> dict[str, Snap]:
    newest: dict[str, Snap] = {}
    for s in snaps:
        if s.camera not in newest or s.time > newest[s.camera].time:
            newest[s.camera] = s
    return newest


def wants(keep_days: int, floor: datetime | None, snap: Snap, newest: Snap) -> bool:
    """Would a destination with this policy keep `snap`?"""
    if floor is not None and snap.time < floor:
        return False
    return keep_days == 0 or snap.time >= newest.time - timedelta(days=keep_days)


def age_cut(snaps: list[Snap], keep_days: int) -> list[Snap]:
    """Snapshots older than `keep_days` before their camera's newest one, which
    itself is never cut (restic's `--keep-within` counts the same way, so an
    outage doesn't delete history)."""
    newest = newest_by_camera(snaps)
    return [
        s
        for s in snaps
        if keep_days and s != newest[s.camera] and not wants(keep_days, None, s, newest[s.camera])
    ]


def quota_cut(
    usage: list[Snap], keep_days: int, max_gb: int, skip: set[str]
) -> tuple[list[Snap], bool, bool]:
    """(snapshots to forget, `short`, `too_small`). `usage` is every snapshot on
    the destination across cameras; `skip` are ids already being forgotten for
    age. Oldest first, until under the low-water mark."""
    if not max_gb:
        return [], False, False
    cap = max_gb * GiB
    used = sum(s.bytes for s in usage if s.id not in skip)
    if used <= cap * QUOTA_HIGH:
        return [], False, False
    newest = newest_by_camera(usage)
    forget: list[Snap] = []
    for s in sorted(usage, key=lambda x: x.time):
        if used <= cap * QUOTA_LOW:
            break
        if s.id in skip or s == newest[s.camera]:
            continue
        forget.append(s)
        used -= s.bytes
    short = any(wants(keep_days, None, s, newest[s.camera]) for s in forget)
    return forget, short, used > cap


def copy_candidates(primary: list[Snap], copy: CopyView) -> list[Snap]:
    """Primary snapshots this copy still lacks and wants, newest first (the
    freshest footage is protected first, and the newest carries the index)."""
    newest = newest_by_camera(primary)
    return sorted(
        (
            s
            for s in primary
            if s.root not in copy.roots.get(s.camera, frozenset())
            and wants(copy.keep_days, copy.floor, s, newest[s.camera])
        ),
        key=lambda x: x.time,
        reverse=True,
    )


def release(
    forget: list[Snap], all_snaps: list[Snap], copies: list[CopyView]
) -> tuple[list[Snap], int]:
    """Drops from `forget` what a copy should still receive. Returns (the
    snapshots that may go, how many were held back)."""
    newest = newest_by_camera(all_snaps)
    ok, held = [], 0
    for s in forget:
        needed = any(
            s.root not in c.roots.get(s.camera, frozenset())
            and wants(c.keep_days, c.floor, s, newest[s.camera])
            for c in copies
        )
        if needed:
            held += 1
        else:
            ok.append(s)
    return ok, held


def decide(
    *,
    camera: str,
    snaps: list[Snap],
    usage: list[Snap],
    keep_days: int,
    max_gb: int,
    copies: list[CopyView] | None = None,
) -> Cut:
    """The cut for one camera's repository on one destination. `snaps` are that
    repository's snapshots, `usage` every camera's snapshots on the destination
    (for the quota), `copies` the destinations that must hold a snapshot first
    (only for the primary)."""
    aged = age_cut(snaps, keep_days)
    skip = {s.id for s in aged}
    quota, short, too_small = quota_cut(usage, keep_days, max_gb, skip)
    floor = max((s.time for s in quota), default=None)
    mine = {s.id for s in snaps}
    wanted = aged + [s for s in quota if s.camera == camera and s.id in mine]
    blocked = 0
    if copies:
        wanted, blocked = release(wanted, snaps, copies)
    return Cut(
        ids=[s.id for s in wanted],
        short=short,
        too_small=too_small,
        blocked=blocked,
        floor=floor,
    )
