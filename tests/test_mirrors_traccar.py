"""The integration reports what Traccar reports, and nothing else.

This replaces an earlier attempt to mark live readings `unavailable` whenever
Traccar said a device was offline. It was well-intentioned — a disconnected car
claiming `ignition: true` is misleading — but it meant inventing a claim Traccar
never made, and in practice it blanked the dashboard for a device whose last
known state was exactly what the user wanted to see.

Traccar already publishes its own view of freshness: `device.status`, governed
by the server's `status.timeout`, plus `fixTime`, `lastUpdate` and `outdated`.
Those are surfaced as entities. Deciding what they mean is the user's job.
"""

from __future__ import annotations

import inspect

from custom_components.traccar_client_extended import binary_sensor, sensor
from custom_components.traccar_client_extended.coordinator import (
    TraccarClientExtendedCoordinator,
)


def test_sensors_do_not_override_availability() -> None:
    """A stale reading is still what Traccar last reported."""
    assert "available" not in sensor.TraccarSensor.__dict__


def test_binary_sensors_do_not_override_availability() -> None:
    assert "available" not in binary_sensor.TraccarBinarySensor.__dict__


def test_coordinator_makes_no_staleness_judgement() -> None:
    """No is_stale(), and no timeout of our own to drive one."""
    assert not hasattr(TraccarClientExtendedCoordinator, "is_stale")

    source = inspect.getsource(TraccarClientExtendedCoordinator)
    assert "stale_after" not in source


def test_descriptions_carry_no_staleness_flag() -> None:
    """The entity descriptions should not encode a policy we no longer have."""
    from custom_components.traccar_client_extended.attributes import (
        TraccarSensorEntityDescription,
    )

    assert "stale_when_offline" not in TraccarSensorEntityDescription.__annotations__


def test_freshness_is_still_visible_as_entities() -> None:
    """Removing the judgement must not remove the evidence.

    These are how a user or an automation can tell that a reading is old, which
    is the whole reason it is safe not to decide for them.
    """
    sensor_keys = {d.key for d in sensor.FIXED_SENSORS}
    assert {"fix_time", "last_update", "device_time", "server_time"} <= sensor_keys

    binary_keys = {d.key for d in binary_sensor.FIXED_BINARY_SENSORS}
    assert {"online", "position_outdated", "position_valid"} <= binary_keys


def test_online_is_on_only_when_traccar_says_online() -> None:
    """Two states, because connectivity has two.

    Traccar has three: `offline` is a connection the protocol saw close, and
    `unknown` is the `status.timeout` sweep dropping a session after nothing was
    heard. Both mean "not connected", and `unknown` is the one most devices
    actually rest in -- a fleet parked overnight is `unknown`, not `offline`.

    Mapping `unknown` to None read as "Unknown" on every card, never fired an
    automation waiting for `off`, and looked like the integration had lost
    track. Traccar is not saying it does not know; it is saying the device is
    not connected. None is reserved for a device record with no status at all.
    """
    description = next(
        d for d in binary_sensor.FIXED_BINARY_SENSORS if d.key == "online"
    )
    assert description.value_fn({"device": {"status": "online"}}) is True
    assert description.value_fn({"device": {"status": "offline"}}) is False
    assert description.value_fn({"device": {"status": "unknown"}}) is False
    assert description.value_fn({"device": {}}) is None


def test_online_still_publishes_traccars_own_status() -> None:
    """Flattening the state must not lose the distinction Traccar draws."""
    description = next(
        d for d in binary_sensor.FIXED_BINARY_SENSORS if d.key == "online"
    )
    attributes = description.attributes_fn({"device": {"status": "unknown"}})

    assert attributes == {"traccar_status": "unknown"}
