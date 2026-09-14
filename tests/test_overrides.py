"""Tests for user-supplied attribute overrides.

The motivating case: Traccar's Teltonika decoder stores unregistered IO
parameters as ``io<id>`` holding a raw integer, so an external voltage arrives
as 12500 millivolts with no indication of what it is. No curation can cover
every vendor's IO numbering, so the mapping has to be overridable.
"""

from __future__ import annotations

from custom_components.traccar_client_extended.attributes import (
    TraccarBinarySensorEntityDescription,
    TraccarSensorEntityDescription,
    describe,
    override_rows,
    parse_overrides,
)
from custom_components.traccar_client_extended.config_flow import check_override_rows
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from .test_coordinator_logic import make_coordinator


def test_teltonika_voltage_override() -> None:
    """The case that prompted this: io800 as millivolts."""
    overrides = parse_overrides("io800: voltage, V, x0.001")
    description = describe("io800", 12500, overrides["io800"])

    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is SensorDeviceClass.VOLTAGE
    assert description.native_unit_of_measurement == "V"
    assert description.scale == 0.001
    # A unit implies something graphable, so statistics should be enabled.
    assert description.state_class is SensorStateClass.MEASUREMENT
    # 12500 mV really is 12.5 V.
    assert 12500 * description.scale == 12.5


def test_override_beats_the_builtin_binary_classification() -> None:
    """Without an override a boolean io key becomes a binary sensor."""
    assert isinstance(describe("io800", True), TraccarBinarySensorEntityDescription)
    overrides = parse_overrides("io800: voltage, V, x0.001")
    assert isinstance(
        describe("io800", True, overrides["io800"]),
        TraccarSensorEntityDescription,
    )


def test_parse_multiple_lines() -> None:
    """Blank lines and comments are skipped."""
    overrides = parse_overrides(
        "\n".join(
            [
                "io800: voltage, V, x0.001",
                "",
                "# a comment",
                "io801: temperature, °C, x0.1, p1",
                "io234: binary",
                "io999: ignore",
            ]
        )
    )
    assert set(overrides) == {"io800", "io801", "io234", "io999"}
    assert overrides["io801"].precision == 1
    assert overrides["io801"].scale == 0.1
    assert overrides["io234"].platform == "binary_sensor"
    assert overrides["io999"].platform == "ignore"


def test_force_binary_sensor() -> None:
    """A numeric value can be forced to a binary sensor."""
    overrides = parse_overrides("io234: binary, motion")
    description = describe("io234", 1, overrides["io234"])
    assert isinstance(description, TraccarBinarySensorEntityDescription)
    assert description.device_class is BinarySensorDeviceClass.MOTION


def test_ignore_suppresses_the_entity() -> None:
    """`ignore` is how a user hides a noisy attribute."""
    overrides = parse_overrides("io999: ignore")
    assert describe("io999", 5, overrides["io999"]) is None


def test_custom_name() -> None:
    """Raw io keys deserve a readable label."""
    overrides = parse_overrides("io800: voltage, V, x0.001, name=External power")
    description = describe("io800", 12500, overrides["io800"])
    assert description.name == "External power"


def test_unit_only_override() -> None:
    """A unit with no device class is valid and still gets statistics."""
    overrides = parse_overrides("io42: rpm")
    description = describe("io42", 900, overrides["io42"])
    assert isinstance(description, TraccarSensorEntityDescription)
    # "rpm" is not a device class, so it is taken as the unit.
    assert description.native_unit_of_measurement == "rpm"
    assert description.state_class is SensorStateClass.MEASUREMENT


def test_ambiguous_device_class_prefers_sensor() -> None:
    """`battery` exists on both platforms; the reading is the useful one."""
    overrides = parse_overrides("io7: battery, %")
    description = describe("io7", 80, overrides["io7"])
    assert isinstance(description, TraccarSensorEntityDescription)
    assert description.device_class is SensorDeviceClass.BATTERY


def test_explicit_binary_wins_for_ambiguous_class() -> None:
    """...unless the user asks for the binary form."""
    overrides = parse_overrides("io7: battery, binary")
    description = describe("io7", True, overrides["io7"])
    assert isinstance(description, TraccarBinarySensorEntityDescription)
    assert description.device_class is BinarySensorDeviceClass.BATTERY


def test_malformed_lines_are_skipped_not_fatal() -> None:
    """This is free text typed into a form; it must not break setup."""
    overrides = parse_overrides(
        "\n".join(["no colon here", ": no key", "io800: voltage, V, x0.001"])
    )
    assert set(overrides) == {"io800"}


def test_empty_input() -> None:
    """The option is optional."""
    assert parse_overrides("") == {}
    assert parse_overrides(None) == {}


def test_scientific_notation_scale() -> None:
    """Some raw values need large or tiny factors."""
    overrides = parse_overrides("io1: voltage, V, x1e-3")
    assert overrides["io1"].scale == 0.001


def test_override_bypasses_denylist_and_cap() -> None:
    """Naming a key is an explicit request for it."""
    coordinator = make_coordinator(
        max_discovered=0,
        data={
            1: {
                "device": {},
                "position": {},
                "geofences": [],
                "attributes": {"raw": "ff", "io800": 12500, "vendorX": 1},
            }
        },
        attribute_overrides=parse_overrides("raw: ignore\nio800: voltage, V, x0.001"),
    )
    discovered = coordinator.discoverable_attributes(1)
    # io800 is unknown and the cap is zero, but it was named explicitly.
    assert "io800" in discovered
    # `raw` is denylisted anyway, and explicitly ignored.
    assert "raw" not in discovered
    # An unnamed unknown key still respects the cap.
    assert "vendorX" not in discovered


# -- The overrides table ------------------------------------------------------
#
# Overrides were a text box parsed by hand. They are rows now, and these pin the
# three things that turn a shape change into data loss: reading the old text,
# round-tripping a row without inventing values, and refusing the device
# class/unit pair that silently kills long-term statistics.


def test_legacy_text_is_still_readable() -> None:
    """An entry saved before the table must not lose its overrides.

    The selector is handed whatever is stored. Hand it the old string and it
    rejects the value, so an entry that is never migrated silently drops every
    override the first time somebody opens the form.
    """
    rows = override_rows("io800: voltage, V, x0.001\nio999: ignore")

    assert rows == [
        {
            "attribute": "io800",
            "treat_as": "sensor",
            "device_class": "voltage",
            "unit": "V",
            "scale": 0.001,
        },
        {"attribute": "io999", "treat_as": "ignore"},
    ]


def test_rows_are_returned_unchanged() -> None:
    """Rows are the storage shape, so reading them back is not a conversion."""
    rows = [{"attribute": "io800", "device_class": "voltage", "unit": "V"}]

    assert override_rows(rows) == rows


def test_a_list_that_is_not_all_rows_does_not_crash_setup() -> None:
    """A stray string among the rows used to take the whole entry down.

    The shape test was "is this a list of rows", answered by looking at the
    first element; anything else fell through to the line parser, which calls
    .strip() on a dict. It runs during setup, so the cost was every entity on
    the entry rather than one bad option.
    """
    assert override_rows([{"attribute": "io1"}, "io2: ignore"]) == [
        {"attribute": "io1"}
    ]


def test_a_row_without_an_attribute_is_dropped() -> None:
    """The table can leave a half-filled row behind; it names nothing."""
    assert override_rows([{"unit": "V"}, {"attribute": "io1"}]) == [
        {"attribute": "io1"}
    ]


def test_empty_cells_do_not_become_settings() -> None:
    """A blank column means "unspecified", not "set to nothing".

    Reading a blank unit as an empty-string unit would strip the unit a curated
    attribute already had, which is the opposite of leaving it alone.
    """
    override = parse_overrides([{"attribute": "io1", "unit": "", "scale": ""}])["io1"]

    assert override.unit is None
    assert override.scale == 1.0
    assert override.precision is None


def test_text_and_rows_produce_the_same_overrides() -> None:
    """The legacy reader is a shim, so it must not mean something different."""
    from_text = parse_overrides("io800: voltage, V, x0.001")
    from_rows = parse_overrides(
        [
            {
                "attribute": "io800",
                "treat_as": "sensor",
                "device_class": "voltage",
                "unit": "V",
                "scale": 0.001,
            }
        ]
    )

    assert from_text == from_rows


# -- Refusing the pair that breaks statistics ---------------------------------


def test_a_mistyped_unit_is_refused() -> None:
    """`volts` is not a unit Home Assistant knows, and it fails silently.

    Home Assistant stores a mismatched device class and unit without complaint,
    then leaves the entity out of long-term statistics -- the value looks right
    on a card and the history is empty. The form is the only place anyone finds
    out, so it has to be the place that refuses.
    """
    rows = [{"attribute": "io800", "device_class": "voltage", "unit": "volts"}]

    assert check_override_rows(rows) == "invalid_device_class_unit"


def test_a_device_class_that_needs_a_unit_must_have_one() -> None:
    assert (
        check_override_rows([{"attribute": "io1", "device_class": "temperature"}])
        == "device_class_needs_unit"
    )


def test_valid_pairs_are_accepted() -> None:
    assert (
        check_override_rows(
            [{"attribute": "a", "device_class": "voltage", "unit": "mV"}]
        )
        is None
    )


def test_a_unitless_device_class_needs_no_unit() -> None:
    """Some device classes take no unit at all; requiring one would be wrong."""
    assert check_override_rows([{"attribute": "a", "device_class": "aqi"}]) is None


def test_a_row_with_no_device_class_is_not_second_guessed() -> None:
    """Scale, name and platform alone are a perfectly good override."""
    assert check_override_rows([{"attribute": "io1", "scale": 0.001}]) is None
