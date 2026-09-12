"""Tests for the fire_test_event action.

This is the shortcut path: it publishes a synthetic event straight into the
integration, for checking an automation without involving Traccar at all.
`tools/mock_gps.py` covers the other direction, driving a real device through
Traccar so its own event logic runs.
"""

from __future__ import annotations

import pytest

from custom_components.traccar_client_extended.const import EVENT_TYPES
from custom_components.traccar_client_extended.services import traccar_event_for


def test_plain_event_maps_back_to_its_traccar_type() -> None:
    assert traccar_event_for("device_overspeed") == ("deviceOverspeed", None)
    assert traccar_event_for("geofence_enter") == ("geofenceEnter", None)


def test_alarm_maps_to_an_alarm_event_with_the_subtype() -> None:
    """The case the whole feature exists for.

    Traccar has no `tow` event type: a tow is an `alarm` event carrying
    `alarm: tow`, so a synthetic one has to be shaped the same way or it would
    test a code path that never runs in production.
    """
    assert traccar_event_for("alarm_tow") == ("alarm", "tow")


def test_snake_case_alarms_map_back_to_camel_case() -> None:
    assert traccar_event_for("alarm_hard_braking") == ("alarm", "hardBraking")
    assert traccar_event_for("alarm_gps_antenna_cut") == ("alarm", "gpsAntennaCut")


def test_generic_alarm_is_still_addressable() -> None:
    assert traccar_event_for("alarm") == ("alarm", None)


@pytest.mark.parametrize("event_type", ["nonsense", "alarm_nonsense", ""])
def test_unknown_types_are_rejected(event_type: str) -> None:
    assert traccar_event_for(event_type) is None


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_advertised_type_round_trips(event_type: str) -> None:
    """Anything the selector offers must be publishable.

    The dropdown is generated from EVENT_TYPES, so an entry that cannot be
    mapped back would be an option that always fails.
    """
    assert traccar_event_for(event_type) is not None


def test_theft_alarms_are_available() -> None:
    for alarm in ("tow", "tampering", "removing", "jamming", "powerCut"):
        snake = traccar_event_for(f"alarm_{alarm}") or traccar_event_for(
            "alarm_" + "".join(f"_{c.lower()}" if c.isupper() else c for c in alarm)
        )
        assert snake is not None, alarm
        assert snake[0] == "alarm"
