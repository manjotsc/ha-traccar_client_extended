"""Tests for the helper functions."""

from __future__ import annotations

import pytest

from custom_components.traccar_client_extended.helpers import (
    get_device,
    get_geofence_ids,
    get_geofences,
    humanize_key,
    parse_timestamp,
)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("totalDistance", "Total distance"),
        ("batteryLevel", "Battery level"),
        ("engineTemp", "Engine temp"),
        ("temp1", "Temp 1"),
        ("io12", "IO 12"),
        ("rssi", "RSSI"),
        ("hdop", "HDOP"),
        ("vin", "VIN"),
        ("obdSpeed", "OBD speed"),
        ("driverUniqueId", "Driver unique ID"),
    ],
)
def test_humanize_key(key: str, expected: str) -> None:
    """Attribute keys become sentence-case names with acronyms preserved."""
    assert humanize_key(key) == expected


def test_parse_timestamp_returns_aware_datetime() -> None:
    """A TIMESTAMP sensor needs a timezone-aware value."""
    parsed = parse_timestamp("2026-02-03T10:11:12.000+00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


def test_parse_timestamp_rejects_traccar_never() -> None:
    """Traccar sends year 1 for 'never', which is not a useful state."""
    assert parse_timestamp("0001-01-01T00:00:00.000+00:00") is None


@pytest.mark.parametrize("value", [None, "", "not a date", 12345, {}])
def test_parse_timestamp_rejects_junk(value) -> None:
    """Bad input yields None rather than raising into the state machine."""
    assert parse_timestamp(value) is None


def test_get_device() -> None:
    """Devices are matched by their numeric id."""
    devices = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert get_device(2, devices)["name"] == "b"
    assert get_device(99, devices) is None


def test_get_geofences_returns_every_match() -> None:
    """Core keeps only the first; overlapping geofences must all survive."""
    geofences = [
        {"id": 1, "name": "Home"},
        {"id": 2, "name": "Neighbourhood"},
        {"id": 3, "name": "Work"},
    ]
    matched = get_geofences(geofences, [1, 2])
    assert [g["name"] for g in matched] == ["Home", "Neighbourhood"]


def test_get_geofences_with_no_matches() -> None:
    """A device outside every geofence yields an empty list, not None."""
    assert get_geofences([{"id": 1, "name": "Home"}], []) == []


def test_get_geofence_ids_prefers_the_position() -> None:
    """Traccar moved geofenceIds from the device to the position in 5.8."""
    device = {"geofenceIds": [9]}
    position = {"geofenceIds": [1, 2]}
    assert get_geofence_ids(device, position) == [1, 2]


def test_get_geofence_ids_falls_back_to_the_device() -> None:
    """Older Traccar releases put it on the device instead."""
    assert get_geofence_ids({"geofenceIds": [9]}, {"geofenceIds": None}) == [9]


def test_get_geofence_ids_when_absent() -> None:
    """Neither side carries the field on very old servers."""
    assert get_geofence_ids({}, {}) == []
