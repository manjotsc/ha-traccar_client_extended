"""Tests for the curated attribute map.

These are the mappings that decide whether an odometer reads in kilometres or
metres and whether a lock reads as locked or unlocked, so they are worth
pinning down explicitly.
"""

from __future__ import annotations

import pytest

from custom_components.traccar_client_extended.attributes import (
    TraccarBinarySensorEntityDescription,
    TraccarSensorEntityDescription,
    describe,
    is_known,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)


def test_odometer_is_metres_displayed_as_kilometres() -> None:
    """Traccar stores distances in metres; users think in kilometres."""
    description = describe("totalDistance", 123456.0)
    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is SensorDeviceClass.DISTANCE
    assert description.native_unit_of_measurement == UnitOfLength.METERS
    assert description.suggested_unit_of_measurement == UnitOfLength.KILOMETERS
    # An odometer only goes up, so the statistics engine should treat it as a
    # total rather than a measurement.
    assert description.state_class is SensorStateClass.TOTAL_INCREASING


def test_engine_hours_are_milliseconds() -> None:
    """Position.java documents `hours` in milliseconds, not hours."""
    description = describe("hours", 3_600_000)
    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is SensorDeviceClass.DURATION
    assert description.native_unit_of_measurement == UnitOfTime.MILLISECONDS
    assert description.suggested_unit_of_measurement == UnitOfTime.HOURS


def test_speed_limit_is_knots_but_obd_speed_is_kmh() -> None:
    """Traccar is inconsistent between these two, which is easy to get wrong."""
    speed_limit = describe("speedLimit", 70)
    obd_speed = describe("obdSpeed", 88)
    assert speed_limit.native_unit_of_measurement == UnitOfSpeed.KNOTS
    assert obd_speed.native_unit_of_measurement == UnitOfSpeed.KILOMETERS_PER_HOUR


def test_battery_level_is_percent_and_battery_is_volts() -> None:
    """`batteryLevel` and `battery` are different quantities in Traccar."""
    level = describe("batteryLevel", 88)
    voltage = describe("battery", 12.6)
    assert level.device_class is SensorDeviceClass.BATTERY
    assert voltage.device_class is SensorDeviceClass.VOLTAGE


def test_lock_is_inverted() -> None:
    """HA's LOCK device class is on=unlocked; Traccar's lock=true is locked."""
    description = describe("lock", True)
    assert isinstance(description, TraccarBinarySensorEntityDescription)
    assert description.invert is True


def test_other_binary_sensors_are_not_inverted() -> None:
    """Only `lock` has mismatched polarity."""
    for key in ("ignition", "motion", "charge", "door"):
        description = describe(key, True)
        assert isinstance(description, TraccarBinarySensorEntityDescription)
        assert description.invert is False, key


@pytest.mark.parametrize(
    ("key", "value", "device_class", "unit"),
    [
        ("temp1", 21.5, SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("temp12", -3.0, SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("adc2", 12.4, SensorDeviceClass.VOLTAGE, "V"),
    ],
)
def test_numbered_families_are_typed(key, value, device_class, unit) -> None:
    """temp1..N and adc1..N are typed without enumerating every index."""
    description = describe(key, value)
    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is device_class
    assert description.native_unit_of_measurement == unit


def test_io_ports_follow_their_value_type() -> None:
    """Traccar's generic IO ports carry either a flag or a reading."""
    assert isinstance(describe("io7", True), TraccarBinarySensorEntityDescription)
    assert isinstance(describe("io8", 42), TraccarSensorEntityDescription)


def test_denylisted_keys_never_become_entities() -> None:
    """Blobs and per-report churn would only bloat the recorder."""
    for key in ("raw", "index", "event", "image", "video", "audio", "dtcs"):
        assert describe(key, "anything") is None, key


def test_unknown_keys_fall_back_by_value_type() -> None:
    """An unrecognised attribute still surfaces rather than being dropped."""
    flag = describe("vendorFlag", False)
    assert isinstance(flag, TraccarBinarySensorEntityDescription)

    numeric = describe("vendorReading", 7)
    assert isinstance(numeric, TraccarSensorEntityDescription)
    assert numeric.state_class is SensorStateClass.MEASUREMENT

    text = describe("vendorText", "hello")
    assert isinstance(text, TraccarSensorEntityDescription)
    assert text.state_class is None


def test_bool_is_not_treated_as_numeric() -> None:
    """bool subclasses int, so ordering in the fallback matters."""
    assert isinstance(describe("someFlag", True), TraccarBinarySensorEntityDescription)


def test_nested_values_are_skipped() -> None:
    """A dict has no sensible single-state representation."""
    assert describe("network", {"cellTowers": []}) is None
    assert describe("someList", [1, 2, 3]) is None


def test_is_known_covers_numbered_families() -> None:
    """Otherwise temp1..temp8 would consume the auto-discovery budget."""
    assert is_known("totalDistance")
    assert is_known("temp9")
    assert is_known("adc1")
    assert is_known("io3")
    assert not is_known("someVendorKey")


def test_alarm_is_not_an_enum() -> None:
    """Vendor decoders emit alarm names outside any list we could declare."""
    description = describe("alarm", "sos")
    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is None
    assert getattr(description, "options", None) is None
