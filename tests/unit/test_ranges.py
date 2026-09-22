"""The Range-header parsing test table, against a 1000-byte resource
(mirrors the real streaming endpoint's frame-aligned 16-byte-vault test at
the HTTP layer)."""

from reovault.web.ranges import ByteRange, parse_range

SIZE = 1000


def test_no_range_header_returns_none():
    assert parse_range(None, SIZE) is None
    assert parse_range("", SIZE) is None


def test_open_ended_range():
    assert parse_range("bytes=0-", SIZE) == ByteRange(0, 999)


def test_single_byte_range():
    assert parse_range("bytes=0-0", SIZE) == ByteRange(0, 0)


def test_ios_safari_probe_range():
    """The exact probe iOS Safari opens playback with. Answering this
    correctly (206, exactly 2 bytes) is what makes <video> work on iPhone."""
    assert parse_range("bytes=0-1", SIZE) == ByteRange(0, 1)


def test_range_spanning_frame_boundaries():
    assert parse_range("bytes=17-34", SIZE) == ByteRange(17, 34)


def test_over_long_end_is_clamped_to_eof():
    """The single most important clamp: an unclamped end would produce a
    Content-Length that doesn't match the actual bytes sent."""
    assert parse_range("bytes=990-2000", SIZE) == ByteRange(990, 999)


def test_suffix_range():
    assert parse_range("bytes=-100", SIZE) == ByteRange(900, 999)


def test_suffix_range_larger_than_file_clamps_to_whole_file():
    assert parse_range("bytes=-5000", SIZE) == ByteRange(0, 999)


def test_start_past_eof_is_unsatisfiable():
    assert parse_range("bytes=1000-1100", SIZE) == "unsatisfiable"


def test_zero_length_suffix_is_unsatisfiable():
    assert parse_range("bytes=-0", SIZE) == "unsatisfiable"


def test_unknown_unit_is_ignored():
    assert parse_range("bananas=1-2", SIZE) is None


def test_garbage_value_is_ignored():
    assert parse_range("bytes=abc", SIZE) is None


def test_multi_range_is_ignored():
    assert parse_range("bytes=0-9,20-29", SIZE) is None


def test_end_before_start_is_ignored():
    assert parse_range("bytes=50-10", SIZE) is None


def test_negative_start_is_ignored():
    assert parse_range("bytes=-10-20", SIZE) is None  # parses as suffix=... actually malformed


def test_empty_resource_any_range_is_unsatisfiable():
    assert parse_range("bytes=0-", 0) == "unsatisfiable"
    assert parse_range("bytes=0-10", 0) == "unsatisfiable"


def test_empty_resource_no_range_returns_none():
    assert parse_range(None, 0) is None
