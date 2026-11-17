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

GB = 1_000_000_000  # decimal, as disks and cloud plans count
QUOTA_HIGH = 0.90  # start cutting above this share of the cap ...
QUOTA_LOW = 0.85  # ... until back under this one (covers the index, the config
# repository and prune's unused space)


@dataclass(frozen=True, slots=True)
class Snap:
    """One snapshot on one destination."""

    camera: str
    id: str  # its own id in that repository
    root: str  # the id it had where it was first made
    time: datetime  # when the snapshot was made
    bytes: int = 0  # the footage it holds (sum of clip sizes)
    footage: datetime | None = None  # its newest clip's start, when the index knows it

    @property
    def age(self) -> datetime:
        """How old the footage is, for retention and the quota. A snapshot made
        today can hold days-old clips (a first run, a backlog after an outage,
        a clip retried for days): counting it by its own time would keep old
        footage long past `keep_days` and cut newer footage before it."""
        return self.footage or self.time


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
    return {s.camera: s for s in sorted(snaps, key=lambda s: s.time)}


def wants(keep_days: int, floor: datetime | None, snap: Snap, newest: Snap) -> bool:
    """Would a destination with this policy keep `snap`?"""
    if floor is not None and snap.age < floor:
        return False
    return keep_days == 0 or snap.age >= newest.time - timedelta(days=keep_days)


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
    cap = max_gb * GB
    used = sum(s.bytes for s in usage if s.id not in skip)
    if used <= cap * QUOTA_HIGH:
        return [], False, False
    newest = newest_by_camera(usage)
    forget: list[Snap] = []
    for s in sorted(usage, key=lambda x: x.age):
        if used <= cap * QUOTA_LOW:
            break
        if s.id in skip or s == newest[s.camera]:
            continue
        forget.append(s)
        used -= s.bytes
    short = any(wants(keep_days, None, s, newest[s.camera]) for s in forget)
    return forget, short, used > cap


def _lacks(copy: CopyView, s: Snap, newest: Snap) -> bool:
    """`copy` should hold `s` but doesn't yet."""
    return s.root not in copy.roots.get(s.camera, frozenset()) and wants(
        copy.keep_days, copy.floor, s, newest
    )


def copy_candidates(primary: list[Snap], copy: CopyView) -> list[Snap]:
    """Primary snapshots this copy still lacks and wants, newest first (the
    freshest footage is protected first, and the newest carries the index)."""
    newest = newest_by_camera(primary)
    return sorted(
        (s for s in primary if _lacks(copy, s, newest[s.camera])),
        key=lambda x: x.age,
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
        if any(_lacks(c, s, newest[s.camera]) for c in copies):
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
    quota_floor: datetime | None = None,
) -> Cut:
    """The cut for one camera's repository on one destination. `snaps` are that
    repository's snapshots, `usage` every camera's snapshots on the destination
    (for the quota), `copies` the destinations that must hold a snapshot first
    (only for the primary).

    The quota is shared, but each camera's thread can only forget its own
    snapshots. So the cut is a footage age (`Cut.floor`, kept by the caller
    as `quota_floor` for a while): every camera's pass forgets its own
    snapshots up to it, even once an earlier pass brought usage back under
    the mark. Without it the first camera to pass paid for the others."""
    aged = age_cut(snaps, keep_days)
    skip = {s.id for s in aged}
    quota, short, too_small = quota_cut(usage, keep_days, max_gb, skip)
    floor = max((s.age for s in quota), default=None)
    if quota_floor is not None and (floor is None or quota_floor > floor):
        floor = quota_floor
    newest = newest_by_camera(snaps)
    trimmed = [
        s
        for s in snaps
        if floor is not None
        and s.camera == camera
        and s.id not in skip
        and s != newest[s.camera]
        and s.age <= floor
    ]
    short = short or any(wants(keep_days, None, s, newest[s.camera]) for s in trimmed)
    wanted = aged + trimmed
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
