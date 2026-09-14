"""Tests that build real entities and read their values.

Every other test in this suite exercises descriptions and coordinator logic. It
turned out none of them ever constructed an entity, so when a refactor deleted
``TraccarSensor._resolve`` the whole suite still passed and py_compile was happy
— the code was valid Python that raised AttributeError the moment Home
Assistant asked a sensor for its state. 380 errors on startup, every entity
unavailable, and nothing here noticed.

These tests close that gap: they instantiate the entity classes and read the
properties Home Assistant reads.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.traccar_client_extended.attributes import (
    AttributeField,
    AttributeOverride,
    describe,
    describe_fields,
    parse_overrides,
)
from custom_components.traccar_client_extended.binary_sensor import (
    FIXED_BINARY_SENSORS,
    TraccarBinarySensor,
)
from custom_components.traccar_client_extended.profiles import parse_profile
from custom_components.traccar_client_extended.sensor import (
    FIXED_SENSORS,
    TraccarSensor,
)

DEVICE = {
    "id": 1,
    "uniqueId": "cobalt",
    "name": "Cobalt",
    "model": "FTC921",
    "status": "online",
    "lastUpdate": "2026-09-07T09:00:00.000+00:00",
}

POSITION = {
    "id": 9,
    "deviceId": 1,
    "protocol": "teltonika",
    "fixTime": "2026-09-07T09:00:00.000+00:00",
    "deviceTime": "2026-09-07T09:00:00.000+00:00",
    "serverTime": "2026-09-07T09:00:01.000+00:00",
    "latitude": 45.5,
    "longitude": -73.6,
    "altitude": 34.0,
    "speed": 4.0,
    "course": 182.0,
    "accuracy": 0.0,
    "valid": True,
    "outdated": False,
    "address": "1 Rue Test",
    "attributes": {
        "ignition": True,
        "batteryLevel": 88,
        "io800": 14583,
        "operator": 302220,
        # A mode byte, a packed word and a hex-dumped string: the three shapes
        # a profile has to reinterpret. 0x099b6549 is a real FTC921 signal
        # reading, and io641 is an ICCID as Traccar's hex dump of its ASCII.
        "io237": 1,
        "io1148": 0x099B6549,
        "io641": "3839313033303030303030303331373139333433",
    },
}


def coordinator():
    # Deep-copied: several tests write a different value into the record they
    # are handed, and the sample must not carry that into the next test.
    position = deepcopy(POSITION)
    return SimpleNamespace(
        data={
            1: {
                "device": DEVICE,
                "position": position,
                "geofences": [{"id": 3, "name": "Home"}, {"id": 4, "name": "Depot"}],
                "attributes": position["attributes"],
            }
        },
        last_update_success=True,
        server_url="http://traccar.example:8082",
        operators={"302": {"220": "Telus Mobility"}},
        hub_identifier=("traccar_client_extended", "server_entry"),
        parent_identifier=lambda _device: (
            "traccar_client_extended",
            "server_entry",
        ),
        async_add_listener=lambda *a, **k: lambda: None,
    )


def sensor(description) -> TraccarSensor:
    entity = TraccarSensor(coordinator(), DEVICE, description)
    entity.hass = SimpleNamespace()
    return entity


def binary(description) -> TraccarBinarySensor:
    entity = TraccarBinarySensor(coordinator(), DEVICE, description)
    entity.hass = SimpleNamespace()
    return entity


def fixed_sensor(key):
    return next(d for d in FIXED_SENSORS if d.key == key)


def fixed_binary(key):
    return next(d for d in FIXED_BINARY_SENSORS if d.key == key)


# -- Every shipped description must produce a readable state ------------------


@pytest.mark.parametrize("description", FIXED_SENSORS, ids=lambda d: d.key)
def test_every_fixed_sensor_reads(description) -> None:
    """The regression: reading state must not raise for any of them."""
    assert sensor(description).native_value is not None or True


@pytest.mark.parametrize("description", FIXED_BINARY_SENSORS, ids=lambda d: d.key)
def test_every_fixed_binary_sensor_reads(description) -> None:
    binary(description).is_on  # noqa: B018 - the point is that it does not raise


def test_every_attribute_sensor_reads() -> None:
    """Attribute entities take the other branch of _resolve."""
    for key, value in POSITION["attributes"].items():
        description = describe(key, value)
        if description in FIXED_SENSORS:
            continue
        entity = (
            sensor(description)
            if hasattr(description, "native_unit_of_measurement")
            and not hasattr(description, "invert")
            else binary(description)
        )
        assert entity.available


# -- Values ------------------------------------------------------------------


def test_position_field_sensor() -> None:
    assert sensor(fixed_sensor("bearing")).native_value == 182.0
    assert sensor(fixed_sensor("speed")).native_value == 4.0
    assert sensor(fixed_sensor("address")).native_value == "1 Rue Test"


def test_labelled_sensor_reads_the_label() -> None:
    """An enumerated reading shows its label, keeping the number alongside."""
    override = AttributeOverride(platform="sensor", values={"1": "GSM"})
    entity = sensor(describe("io237", 1, override))
    assert entity.native_value == "GSM"
    assert entity.extra_state_attributes == {"raw_value": 1}


def test_unlabelled_value_keeps_the_number() -> None:
    """A value the profile does not list must not become Unknown."""
    override = AttributeOverride(platform="sensor", values={"1": "GSM"})
    entity = sensor(describe("io237", 1, override))
    entity.coordinator.data[1]["attributes"]["io237"] = 9
    assert entity.native_value == 9


def test_packed_field_sensors_read_their_byte() -> None:
    """The whole point: one attribute, four sensors, real units.

    0x099b6549 is a real FTC921 reading whose firmware log printed
    RSSI -73 dBm, RSRP -101 dBm, SINR +11 dB, RSRQ -9 dB.
    """
    profile = parse_profile(
        json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "custom_components"
                / "traccar_client_extended"
                / "device_profiles"
                / "teltonika_ftc921.json"
            ).read_text(encoding="utf-8")
        )
    )
    override = profile.overrides["io1148"]
    reading = {
        description.key: sensor(description).native_value
        for description in describe_fields("io1148", override)
    }
    assert reading == {
        "attr_io1148_rssi": -73,
        "attr_io1148_rsrp": -101,
        "attr_io1148_sinr": pytest.approx(11.0),
        "attr_io1148_rsrq": -9,
    }


def test_decoded_sensor_reads_as_text_and_keeps_the_raw() -> None:
    """Traccar hex-dumps variable-length parameters; the profile undoes it."""
    override = AttributeOverride(decode="hex_ascii")
    entity = sensor(describe("io641", POSITION["attributes"]["io641"], override))
    assert entity.native_value == "89103000000031719343"
    assert entity.extra_state_attributes == {
        "raw_value": "3839313033303030303030303331373139333433"
    }


def test_packed_bit_field_becomes_a_binary_sensor() -> None:
    """0x099b6549 has bit 0 set and bit 1 clear."""
    override = AttributeOverride(
        fields={
            "on": AttributeField(bit=0, width=1, platform="binary_sensor"),
            "off": AttributeField(bit=1, width=1, platform="binary_sensor"),
        }
    )
    states = {
        description.key: binary(description).is_on
        for description in describe_fields("io1148", override)
    }
    assert states == {"attr_io1148_on": True, "attr_io1148_off": False}


def test_packed_signed_field_reads_below_zero() -> None:
    override = AttributeOverride(
        fields={"sinr": AttributeField(bit=16, width=8, signed=True)}
    )
    # Byte 2 of the sample is 0x9b = 155, which as a signed byte is -101.
    entity = sensor(describe_fields("io1148", override)[0])
    assert entity.native_value == -101


def test_packed_field_sensor_is_unavailable_without_an_integer() -> None:
    """A missing parameter must not read as byte zero."""
    override = AttributeOverride(
        fields={"rssi": AttributeField(bit=0, width=8, scale=-1)}
    )
    entity = sensor(describe_fields("io1148", override)[0])
    entity.coordinator.data[1]["attributes"]["io1148"] = "n/a"
    assert entity.native_value is None


def test_operator_sensor_decodes_the_plmn() -> None:
    """A numeric network code reads as a name, with the code kept alongside."""
    entity = sensor(describe("operator", 302220))
    assert entity.native_value == "Telus Mobility"
    assert entity.extra_state_attributes == {"traccar_operator": 302220}


def test_unknown_operator_keeps_the_raw_code() -> None:
    """An unlisted code must not become Unknown."""
    stub = coordinator()
    stub.operators = {}
    entity = TraccarSensor(stub, DEVICE, describe("operator", 302220))
    entity.hass = SimpleNamespace()
    assert entity.native_value == 302220
    assert entity.extra_state_attributes == {"traccar_operator": 302220}


def test_operator_override_turns_the_decode_off() -> None:
    """An override asking for a number is asking for the number."""
    override = parse_overrides(["operator: sensor"])["operator"]
    entity = sensor(describe("operator", 302220, override))
    assert entity.native_value == 302220


def test_timestamp_sensor_returns_aware_datetime() -> None:
    value = sensor(fixed_sensor("fix_time")).native_value
    assert isinstance(value, datetime)
    assert value.tzinfo is not None


def test_device_field_sensor() -> None:
    assert isinstance(sensor(fixed_sensor("last_update")).native_value, datetime)


def test_attribute_sensor_reads_from_attributes() -> None:
    assert sensor(describe("batteryLevel", 88)).native_value == 88


def test_scaled_attribute_sensor() -> None:
    """The io800 case: raw millivolts scaled into volts."""
    overrides = parse_overrides("io800: voltage, V, x0.001")
    assert sensor(describe("io800", 14583, overrides["io800"])).native_value == 14.583


def test_geofence_sensor_lists_every_match() -> None:
    entity = sensor(fixed_sensor("geofence"))
    assert entity.native_value == "Home"
    assert entity.extra_state_attributes == {"geofences": ["Home", "Depot"]}


def test_binary_sensor_value() -> None:
    assert binary(describe("ignition", True)).is_on is True


def test_binary_sensor_inversion() -> None:
    """Traccar's lock=true means locked; HA's LOCK class means unlocked."""
    entity = TraccarBinarySensor(
        SimpleNamespace(
            data={
                1: {
                    "device": DEVICE,
                    "position": POSITION,
                    "geofences": [],
                    "attributes": {"lock": True},
                }
            },
            last_update_success=True,
            server_url="http://x",
            hub_identifier=("traccar_client_extended", "server_entry"),
            parent_identifier=lambda _device: (
                "traccar_client_extended",
                "server_entry",
            ),
            async_add_listener=lambda *a, **k: lambda: None,
        ),
        DEVICE,
        describe("lock", True),
    )
    assert entity.is_on is False


def test_online_binary_sensor() -> None:
    assert binary(fixed_binary("online")).is_on is True


# -- Availability -------------------------------------------------------------


def test_entities_are_available_when_the_device_is_known() -> None:
    assert sensor(fixed_sensor("speed")).available


def test_offline_device_stays_available() -> None:
    """We report what Traccar reports; a stale reading is still a reading."""
    c = coordinator()
    c.data[1]["device"] = {**DEVICE, "status": "offline"}
    entity = TraccarSensor(c, DEVICE, fixed_sensor("speed"))
    assert entity.available
    assert entity.native_value == 4.0


def test_unknown_device_is_unavailable() -> None:
    c = coordinator()
    c.data = {}
    entity = TraccarSensor(c, DEVICE, fixed_sensor("speed"))
    assert not entity.available
    assert entity.native_value is None


def test_failed_refresh_makes_entities_unavailable() -> None:
    c = coordinator()
    c.last_update_success = False
    assert not TraccarSensor(c, DEVICE, fixed_sensor("speed")).available


# -- Registry identity --------------------------------------------------------


def test_unique_ids_are_namespaced_per_device_and_key() -> None:
    assert sensor(fixed_sensor("speed"))._attr_unique_id == "cobalt_speed"
    assert sensor(describe("io800", 1))._attr_unique_id == "cobalt_attr_io800"


def test_every_device_hangs_off_the_server_hub() -> None:
    """One hub device, so a fleet does not sprawl across the integration page.

    The Traccar server is the honest parent: it is the one thing every tracker
    genuinely connects through.
    """
    entity = sensor(fixed_sensor("bearing"))
    assert entity.device_info["via_device"] == (
        "traccar_client_extended",
        "server_entry",
    )


def test_device_info_identifies_the_traccar_device() -> None:
    info = sensor(fixed_sensor("speed"))._attr_device_info
    assert info["serial_number"] == "cobalt"
    assert info["model"] == "FTC921"
