"""Pure HTTP Range-header parsing for `/recordings/{id}/stream`. No I/O, so
every case is unit-testable byte-exact.

RFC 9110 permits a server to ignore a Range it doesn't understand and serve
the whole entity with `200`; every "give up" path here does exactly that by
returning `None`, rather than guessing or raising. The only path that must
answer `416` is a range that is genuinely out of bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Unsatisfiable = Literal["unsatisfiable"]


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int  # inclusive


def parse_range(header: str | None, size: int) -> ByteRange | None | Unsatisfiable:
    """`header` is the raw `Range` request header, `size` is the resource's
    total byte length (the plaintext size, from the DB or the sidecar).

    - No header, an unrecognized unit, a malformed spec, or a multi-range
      request (`bytes=0-9,20-29`) -> `None` (serve `200`, whole body). A
      single-range player never sends a multi-range request; implementing
      `multipart/byteranges` for one is not worth it.
    - A range whose start is at or past `size`, or a zero-length suffix
      (`bytes=-0`), -> `"unsatisfiable"` (serve `416`).
    - Otherwise -> a `ByteRange`, with `end` **clamped to `size - 1`**. The
      clamp is mandatory: an unclamped `end` produces a `Content-Length`
      that doesn't match what's actually sent, and h11 kills the connection
      mid-transfer rather than degrading gracefully.
    """
    if not header:
        return None
    header = header.strip()
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :]
    if "," in spec:
        return None  # multi-range
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")

    if start_s == "" and end_s == "":
        return None

    if start_s == "":
        # Suffix range: the last N bytes.
        try:
            suffix = int(end_s)
        except ValueError:
            return None
        if suffix <= 0:
            return "unsatisfiable"
        if size == 0:
            return "unsatisfiable"
        start = max(size - suffix, 0)
        return ByteRange(start, size - 1)

    try:
        start = int(start_s)
    except ValueError:
        return None
    if start < 0:
        return None
    if size == 0 or start >= size:
        return "unsatisfiable"

    if end_s == "":
        return ByteRange(start, size - 1)

    try:
        end = int(end_s)
    except ValueError:
        return None
    if end < start:
        return None
    return ByteRange(start, min(end, size - 1))
