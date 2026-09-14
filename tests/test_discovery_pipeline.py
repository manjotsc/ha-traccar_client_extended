"""End-to-end check of the discovery pipeline against realistic payloads.

Runs a device's real attribute set through the same two steps the platforms use
-- ``discoverable_attributes`` then ``describe`` -- and asserts what entities
come out. This is the behaviour a user actually sees.
"""

from __future__ import annotations

from custom_components.traccar_client_extended.attributes import (
    TraccarBinarySensorEntityDescription,
    TraccarSensorEntityDescription,
    describe,
)

from .test_coordinator_logic import make_coordinator

# What a phone running Traccar Client reports through a Traccar Server.
PHONE_ATTRIBUTES = {
    "batteryLevel": 82.0,
    "distance": 12.4,
    "totalDistance": 154233.0,
    "motion": True,
    "charge": False,
    "ip": "203.0.113.7",
}

# What an OBD dongle reports. Almost no overlap with the phone.
OBD_ATTRIBUTES = {
    "ignition": True,
    "rpm": 1850,
    "obdSpeed": 64,
    "engineLoad": 42,
    "coolantTemp": 89,
    "fuelLevel": 55,
    "totalDistance": 8_412_000.0,
    "hours": 5_400_000,
    "power": 13.8,
    "battery": 12.4,
    "sat": 11,
    "hdop": 0.9,
    "vin": "WVWZZZ1JZXW000001",
    "alarm": "hardBraking",
    "temp1": 21.5,
    "temp2": 19.0,
    "io3": True,
    "raw": "0102030405",
    "acmeProprietary": 99,
}


def split(attributes: dict) -> tuple[list[str], list[str], list[str]]:
    """Return (sensor keys, binary keys, skipped keys) for an attribute set."""
    coordinator = make_coordinator(
        data={
            1: {
                "device": {},
                "position": {},
                "geofences": [],
                "attributes": attributes,
            }
        }
    )
    sensors, binaries, skipped = [], [], []
    discoverable = coordinator.discoverable_attributes(1)
    for key in attributes:
        if key not in discoverable:
            skipped.append(key)
            continue
        description = describe(key, attributes[key])
        if isinstance(description, TraccarBinarySensorEntityDescription):
            binaries.append(key)
        elif isinstance(description, TraccarSensorEntityDescription):
            sensors.append(key)
        else:
            skipped.append(key)
    return sorted(sensors), sorted(binaries), sorted(skipped)


def test_phone_payload() -> None:
    """A phone yields a small, fully curated entity set."""
    sensors, binaries, skipped = split(PHONE_ATTRIBUTES)
    assert sensors == ["batteryLevel", "distance", "ip", "totalDistance"]
    assert binaries == ["charge", "motion"]
    assert skipped == []


def test_obd_payload() -> None:
    """An OBD dongle yields far more, and `raw` never becomes an entity."""
    sensors, binaries, skipped = split(OBD_ATTRIBUTES)

    assert "raw" in skipped, "blob attributes must stay out of the recorder"
    assert binaries == ["ignition", "io3"]
    # The vendor-specific key still surfaces rather than being dropped.
    assert "acmeProprietary" in sensors
    for expected in ("rpm", "obdSpeed", "coolantTemp", "hours", "temp1", "temp2"):
        assert expected in sensors, expected


def test_every_discovered_key_yields_exactly_one_entity() -> None:
    """No key may produce both a sensor and a binary sensor."""
    for attributes in (PHONE_ATTRIBUTES, OBD_ATTRIBUTES):
        sensors, binaries, _ = split(attributes)
        assert not set(sensors) & set(binaries)


def test_unique_ids_do_not_collide() -> None:
    """Entity keys must be unique per device across both platforms."""
    keys = set()
    for key, value in OBD_ATTRIBUTES.items():
        description = describe(key, value)
        if description is None:
            continue
        assert description.key not in keys, f"duplicate entity key for {key}"
        keys.add(description.key)


def test_attribute_keys_cannot_collide_with_fixed_keys() -> None:
    """Attribute entities are namespaced, so `attr_speed` != the speed sensor."""
    from custom_components.traccar_client_extended.binary_sensor import (
        FIXED_BINARY_SENSORS,
    )
    from custom_components.traccar_client_extended.sensor import FIXED_SENSORS

    fixed = {d.key for d in FIXED_SENSORS} | {d.key for d in FIXED_BINARY_SENSORS}
    attribute_keys = {
        describe(key, value).key
        for key, value in {**PHONE_ATTRIBUTES, **OBD_ATTRIBUTES}.items()
        if describe(key, value) is not None
    }
    assert not fixed & attribute_keys
