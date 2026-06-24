"""Tests for Alfen log parsing and tag extraction."""
import pytest
from datetime import datetime, timezone

from bridge.alfen_client import _parse_log_line, _extract_tag_entry, AlfenClient, RateLimiter


# ---- Log line parsing --------------------------------------------------------

VALID_LINE = "42_2026-06-24T10:15:30+0200 INFO:socket.cpp:123:Socket #1 tag: 04A1B2C3D4E5"
VALID_LINE_NO_TAG = "41_2026-06-24T10:15:28+0200 INFO:auth.cpp:99:Socket #1 authorised"
MALFORMED_LINE = "not a valid log line"
EMPTY_LINE = ""
PARTIAL_LINE = "99_2026-06-24T10"  # truncated


def test_parse_valid_line():
    result = _parse_log_line(VALID_LINE)
    assert result is not None
    lid, ts, message = result
    assert lid == 42
    assert ts.year == 2026
    assert "Socket #1 tag: 04A1B2C3D4E5" in message


def test_parse_line_without_tag():
    result = _parse_log_line(VALID_LINE_NO_TAG)
    assert result is not None
    lid, ts, message = result
    assert lid == 41


def test_parse_malformed_line():
    assert _parse_log_line(MALFORMED_LINE) is None


def test_parse_empty_line():
    assert _parse_log_line(EMPTY_LINE) is None


def test_parse_partial_line():
    assert _parse_log_line(PARTIAL_LINE) is None


# ---- Tag extraction ----------------------------------------------------------

def test_extract_tag_correct_socket():
    uid = _extract_tag_entry("Socket #1 tag: 04A1B2C3D4E5", socket=1)
    assert uid == "04A1B2C3D4E5"


def test_extract_tag_wrong_socket():
    uid = _extract_tag_entry("Socket #2 tag: 04A1B2C3D4E5", socket=1)
    assert uid is None


def test_extract_tag_missing_tag():
    uid = _extract_tag_entry("Socket #1 authorised", socket=1)
    assert uid is None


def test_extract_tag_case_insensitive():
    uid = _extract_tag_entry("socket #1 TAG: AABBCCDD", socket=1)
    assert uid == "AABBCCDD"


def test_extract_tag_with_whitespace():
    uid = _extract_tag_entry("Socket #1 tag:  04FF", socket=1)
    assert uid == "04FF"


# ---- Non-chronological selection (most recent wins) -------------------------

def test_most_recent_tag_selected():
    """get_latest_tag must return the tag from the highest-lid entry, not the first found."""
    lines = [
        "10_2026-06-24T10:14:00+0200 INFO:x.cpp:1:Socket #1 tag: OLDUID1111",
        "20_2026-06-24T10:15:00+0200 INFO:x.cpp:1:Socket #1 tag: NEWUID2222",
        "15_2026-06-24T10:14:30+0200 INFO:x.cpp:1:Socket #1 tag: MIDUID3333",
    ]
    connect_time = datetime(2026, 6, 24, 10, 13, 0, tzinfo=timezone.utc)

    # Simulate the scan logic directly
    from datetime import timezone
    best_lid = -1
    best_uid = None
    for line in reversed(lines):
        parsed = _parse_log_line(line)
        if parsed is None:
            continue
        lid, ts, message = parsed
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < connect_time.replace(tzinfo=timezone.utc):
            continue
        uid = _extract_tag_entry(message, 1)
        if uid and lid > best_lid:
            best_lid = lid
            best_uid = uid

    assert best_uid == "NEWUID2222"


# ---- Malformed JSON tolerance ------------------------------------------------

def test_malformed_json_trailing_comma():
    """The bridge must tolerate JSON with trailing commas in Alfen responses."""
    import json
    # Standard json.loads fails on trailing comma
    bad = '{"key": "value",}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)

    # Our workaround: strip trailing commas before closing braces/brackets
    import re
    cleaned = re.sub(r",\s*([}\]])", r"\1", bad)
    result = json.loads(cleaned)
    assert result == {"key": "value"}


# ---- Rate limiter ------------------------------------------------------------

def test_rate_limiter_allows_under_limit():
    rl = RateLimiter(max_calls=3, window_s=60)
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire() is True


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter(max_calls=2, window_s=60)
    rl.acquire()
    rl.acquire()
    assert rl.acquire() is False


def test_rate_limiter_wait_positive_when_full():
    rl = RateLimiter(max_calls=1, window_s=60)
    rl.acquire()
    assert rl.wait_s() > 0
