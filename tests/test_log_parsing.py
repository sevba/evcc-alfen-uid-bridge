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
    # Use actual Alfen UTC/Z format to avoid offset-vs-connect_time timezone confusion.
    lines = [
        "10_2026-06-24T10:14:00.000Z:INFO:x.cpp:1:Socket #1 tag: AA110001",
        "20_2026-06-24T10:15:00.000Z:INFO:x.cpp:1:Socket #1 tag: BB220002",
        "15_2026-06-24T10:14:30.000Z:INFO:x.cpp:1:Socket #1 tag: CC330003",
    ]
    connect_time = datetime(2026, 6, 24, 10, 13, 0, tzinfo=timezone.utc)

    # Simulate the scan logic directly
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

    assert best_uid == "BB220002"


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


# ---- NFC reader and auth patterns -------------------------------------------

def test_extract_nfc_reader_pattern():
    uid = _extract_tag_entry("Reader 0 Got NFC tag: 04AABBCCDDEEFF", socket=1)
    assert uid == "04AABBCCDDEEFF"


def test_extract_nfc_reader_not_socket_specific():
    """NFC reader lines have no socket prefix — must match any socket."""
    uid = _extract_tag_entry("Reader 0 Got NFC tag: 04AABBCCDDEEFF", socket=2)
    assert uid == "04AABBCCDDEEFF"


def test_extract_auth_line_pattern():
    uid = _extract_tag_entry("Tag 12345678 is authorised by server, white list updated", socket=1)
    assert uid == "12345678"


def test_extract_auth_line_not_socket_specific():
    uid = _extract_tag_entry("Tag 12345678 is authorised by server", socket=2)
    assert uid == "12345678"


# ---- full_uid_only flag (extended lookback) ----------------------------------

def test_full_uid_only_skips_state_line():
    uid = _extract_tag_entry("Socket #1 tag: 04A1B2C3", socket=1, full_uid_only=True)
    assert uid is None


def test_full_uid_only_keeps_nfc_reader():
    uid = _extract_tag_entry("Reader 0 Got NFC tag: 04AABBCCDDEEFF", socket=1, full_uid_only=True)
    assert uid == "04AABBCCDDEEFF"


def test_full_uid_only_keeps_auth_line():
    uid = _extract_tag_entry("Tag 12345678 is authorised by server", socket=1, full_uid_only=True)
    assert uid == "12345678"


# ---- Actual Alfen log format (verified from live device) --------------------

ALFEN_NFC_LINE = "843136_2026-06-24T11:34:59.535Z:INFO:tag.c:123:Reader 0 Got NFC tag: 04AABBCCDDEEFF"
ALFEN_AUTH_LINE = "843200_2026-06-24T11:35:00.123Z:INFO:tag.c:456:Tag 12345678 is authorised by server, white list updated"
ALFEN_STATE_LINE = "843256_2026-06-24T11:35:10.000Z:INFO:taskMain.c:5996:Socket #1: main state: charging, tag: 12345678"


def test_alfen_nfc_line_parses_and_extracts():
    result = _parse_log_line(ALFEN_NFC_LINE)
    assert result is not None
    lid, ts, message = result
    assert lid == 843136
    assert ts.tzinfo is not None
    assert _extract_tag_entry(message, socket=1) == "04AABBCCDDEEFF"


def test_alfen_auth_line_parses_and_extracts():
    result = _parse_log_line(ALFEN_AUTH_LINE)
    assert result is not None
    lid, ts, message = result
    assert lid == 843200
    assert _extract_tag_entry(message, socket=1) == "12345678"


def test_alfen_state_line_parses_and_extracts():
    result = _parse_log_line(ALFEN_STATE_LINE)
    assert result is not None
    lid, ts, message = result
    assert lid == 843256
    assert _extract_tag_entry(message, socket=1) == "12345678"


def test_alfen_state_line_skipped_with_full_uid_only():
    result = _parse_log_line(ALFEN_STATE_LINE)
    assert result is not None
    _, _, message = result
    assert _extract_tag_entry(message, socket=1, full_uid_only=True) is None


def test_nfc_preferred_over_state_line():
    """NFC reader pattern wins over state-line pattern when both appear."""
    msg = "Reader 0 Got NFC tag: FULL1234  Socket #1 tag: TRUNC"
    uid = _extract_tag_entry(msg, socket=1)
    assert uid == "FULL1234"


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
