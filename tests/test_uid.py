"""Tests for UID normalisation and map matching."""
from bridge.config import normalise_uid, Config


def test_normalise_strips_separators():
    assert normalise_uid("04:A1:B2:C3") == "04A1B2C3"
    assert normalise_uid("04-A1-B2-C3") == "04A1B2C3"
    assert normalise_uid("04 A1 B2 C3") == "04A1B2C3"


def test_normalise_uppercase():
    assert normalise_uid("04a1b2c3") == "04A1B2C3"


def test_normalise_already_clean():
    assert normalise_uid("04A1B2C3") == "04A1B2C3"


def test_map_lookup_case_insensitive():
    """UIDs in the map are stored normalised; incoming UIDs are normalised before lookup."""
    raw_map = '{"04:a1:b2:c3": "bmw320e"}'
    import json, re
    data = json.loads(raw_map)
    uid_map = {normalise_uid(k): v for k, v in data.items()}

    # Lookup with different formatting
    assert uid_map.get(normalise_uid("04A1B2C3")) == "bmw320e"
    assert uid_map.get(normalise_uid("04:A1:B2:C3")) == "bmw320e"
    assert uid_map.get(normalise_uid("04-a1-b2-c3")) == "bmw320e"


def test_map_unknown_uid_returns_none():
    uid_map = {"04A1B2C3": "bmw320e"}
    assert uid_map.get(normalise_uid("DEADBEEF")) is None
