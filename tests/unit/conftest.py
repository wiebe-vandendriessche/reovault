from datetime import UTC, datetime

from reovault.models import RemoteRecording


def make_recording(
    name: str = "20260417_120000.mp4",
    start: datetime | None = None,
    *,
    end: datetime | None = None,
    duration_s: float | None = None,
    rec_type: str | None = "md",
    stream: str | None = "main",
    size: int | None = 1024,
    raw_metadata: dict | None = None,
) -> RemoteRecording:
    """A `RemoteRecording` factory for tests that don't care about most of
    its fields. `start` defaults to a fixed timestamp rather than `now()` so
    tests stay deterministic; `raw_metadata` defaults to `{"fileName": name}`
    since that's the shape `reolink-cli` actually returns."""
    return RemoteRecording(
        remote_name=name,
        start_utc=start or datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC),
        end_utc=end,
        duration_s=duration_s,
        rec_type=rec_type,
        stream=stream,
        remote_size=size,
        raw_metadata=raw_metadata if raw_metadata is not None else {"fileName": name},
    )
