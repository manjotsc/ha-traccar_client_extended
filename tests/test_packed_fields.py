"""Tests for values packed inside one Traccar attribute.

Teltonika's AVL 1148 carries RSSI, RSRP, SINR and RSRQ in the four bytes of one
32-bit word. Published as a single number it was a sensor reading 272725845
dBm, with a device class, going into long-term statistics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.traccar_client_extended.attributes import (
    AttributeField,
    AttributeOverride,
    TraccarBinarySensorEntityDescription,
    describe,
    describe_fields,
    extract_bits,
)
from custom_components.traccar_client_extended.profiles import (
    ProfileError,
    _parse_attribute,
    parse_profile,
)

#: One real reading from an FTC921 offline log: RSSI -73 dBm, RSRP -101 dBm,
#: SINR +11 dB, RSRQ -9 dB, which is what the firmware printed for it.
SAMPLE = 0x099B6549

FTC921 = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "traccar_client_extended"
    / "device_profiles"
    / "teltonika_ftc921.json"
)


# -- Extraction ---------------------------------------------------------------


def test_bytes_count_from_the_least_significant() -> None:
    assert extract_bits(SAMPLE, 0, 8) == 0x49
    assert extract_bits(SAMPLE, 8, 8) == 0x65
    assert extract_bits(SAMPLE, 16, 8) == 0x9B
    assert extract_bits(SAMPLE, 24, 8) == 0x09


def test_a_run_of_bits_need_not_be_byte_aligned() -> None:
    """0x49 is 0b01001001, so bit 0 is set, bit 1 is not, bits 3..5 are 0b001."""
    assert extract_bits(SAMPLE, 0, 1) == 1
    assert extract_bits(SAMPLE, 1, 1) == 0
    assert extract_bits(SAMPLE, 3, 3) == 0b001


def test_a_field_can_span_two_bytes() -> None:
    assert extract_bits(SAMPLE, 0, 16) == 0x6549


def test_signed_fields_count_below_zero() -> None:
    """A temperature byte reading 251 is -5, not 251."""
    assert extract_bits(251, 0, 8, signed=True) == -5
    assert extract_bits(251, 0, 8) == 251
    assert extract_bits(0x7F, 0, 8, signed=True) == 127
    assert extract_bits(0xFFFF, 0, 16, signed=True) == -1


def test_no_width_passes_the_value_through() -> None:
    assert extract_bits(SAMPLE, 0, None) == SAMPLE


@pytest.mark.parametrize("value", [None, True, False, "0x099b6549", 1.5, {}])
def test_non_integers_extract_to_none(value: object) -> None:
    """A decoder sending this as text must not produce a number that looks real."""
    assert extract_bits(value, 0, 8) is None


# -- Descriptions -------------------------------------------------------------


def test_fields_become_their_own_descriptions() -> None:
    override = AttributeOverride(
        fields={
            "rssi": AttributeField(bit=0, width=8),
            "rsrp": AttributeField(bit=8, width=8),
        }
    )
    keys = [d.key for d in describe_fields("io1148", override)]
    assert keys == ["attr_io1148_rssi", "attr_io1148_rsrp"]


def test_fields_read_the_parent_attribute() -> None:
    override = AttributeOverride(fields={"rssi": AttributeField(bit=0, width=8)})
    assert describe_fields("io1148", override)[0].attribute_key == "io1148"


def test_no_fields_means_no_descriptions() -> None:
    assert describe_fields("io1148", None) == []
    assert describe_fields("io1148", AttributeOverride()) == []


# -- Bit ranges, signedness and platform --------------------------------------


def test_byte_is_shorthand_for_a_bit_range() -> None:
    by_byte = _parse_attribute("io1", {"fields": {"a": {"byte": 2}}})
    by_bits = _parse_attribute("io1", {"fields": {"a": {"bits": [16, 8]}}})
    assert by_byte.fields == by_bits.fields


def test_a_bit_field_becomes_a_binary_sensor() -> None:
    override = _parse_attribute(
        "io1",
        {
            "fields": {
                "towing": {
                    "bits": [3, 1],
                    "platform": "binary_sensor",
                    "device_class": "problem",
                }
            }
        },
    )
    description = describe_fields("io1", override)[0]
    assert isinstance(description, TraccarBinarySensorEntityDescription)
    assert description.device_class == "problem"
    assert description.bit_offset == 3
    assert description.bit_width == 1


def test_a_bit_field_can_be_labelled() -> None:
    override = _parse_attribute(
        "io1", {"fields": {"mode": {"bits": [4, 3], "values": {"1": "Active"}}}}
    )
    description = describe_fields("io1", override)[0]
    assert description.value_labels == {"1": "Active"}
    # Labels make the state text, so there is nothing to record.
    assert description.state_class is None


def test_a_signed_field_survives_parsing() -> None:
    override = _parse_attribute(
        "io1", {"fields": {"t": {"bits": [8, 16], "signed": True}}}
    )
    assert override.fields is not None
    assert override.fields["t"].signed is True


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"byte": 0, "bits": [0, 8]},
        {"bits": [0, 8, 1]},
        {"bits": [0]},
        {"bits": [-1, 8]},
        {"bits": [0, 0]},
        {"bits": [60, 8]},
        {"bits": [0, True]},
        {"bits": "0,8"},
        {"byte": 0, "signed": "yes"},
        {"bits": [0, 1], "signed": True},
        {"byte": 0, "platform": "ignore"},
        {"byte": 0, "platform": "binary_sensor", "unit": "V"},
        {"byte": 0, "platform": "binary_sensor", "scale": 2},
        {"byte": 0, "values": {"1": "On"}, "unit": "V"},
    ],
)
def test_malformed_bit_specs_are_refused(spec: dict) -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io1", {"fields": {"a": spec}})


# -- Presentation -------------------------------------------------------------


def test_fields_take_a_category_and_an_enabled_flag() -> None:
    override = _parse_attribute(
        "io1148",
        {
            "fields": {
                "rssi": {
                    "byte": 0,
                    "entity_category": "diagnostic",
                    "enabled_by_default": False,
                }
            }
        },
    )
    description = describe_fields("io1148", override)[0]
    assert description.entity_category == "diagnostic"
    assert description.entity_registry_enabled_default is False


def test_fields_are_visible_and_enabled_unless_said_otherwise() -> None:
    override = _parse_attribute("io1148", {"fields": {"rssi": {"byte": 0}}})
    description = describe_fields("io1148", override)[0]
    assert description.entity_category is None
    assert description.entity_registry_enabled_default is True


@pytest.mark.parametrize(
    "spec",
    [
        {"byte": 0, "entity_category": "config"},
        {"byte": 0, "entity_category": "hidden"},
        {"byte": 0, "enabled_by_default": "no"},
    ],
)
def test_malformed_field_presentation_is_refused(spec: dict) -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io1148", {"fields": {"rssi": spec}})


# -- Profile validation -------------------------------------------------------


def test_fields_parse() -> None:
    override = _parse_attribute(
        "io1148", {"fields": {"rssi": {"byte": 0, "scale": -1, "unit": "dBm"}}}
    )
    assert override.fields is not None
    assert override.fields["rssi"] == AttributeField(
        bit=0, width=8, scale=-1.0, unit="dBm"
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"fields": []},
        {"fields": {}},
        {"fields": {"rssi": {}}},
        {"fields": {"rssi": {"byte": 8}}},
        {"fields": {"rssi": {"byte": -1}}},
        {"fields": {"rssi": {"byte": True}}},
        {"fields": {"rssi": "byte 0"}},
        {"fields": {"RSSI": {"byte": 0}}},
        {"fields": {"rssi ": {"byte": 0}}},
        {"fields": {"rssi": {"byte": 0, "scale": "x"}}},
        {"fields": {"rssi": {"byte": 0, "offset": "x"}}},
        {"fields": {"rssi": {"byte": 0, "precision": 1.5}}},
        # The pairing that silently disables statistics, same rule as elsewhere.
        # (`signal_strength` accepts both dB and dBm, so it is not the example.)
        {"fields": {"rssi": {"byte": 0, "device_class": "temperature", "unit": "dBm"}}},
    ],
)
def test_malformed_fields_are_refused(raw: dict) -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io1148", raw)


def test_offset_must_be_numeric() -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io70", {"offset": "warm"})


# -- The shipped FTC921 mapping -----------------------------------------------


def test_ftc921_packs_four_signal_readings() -> None:
    override = parse_profile(json.loads(FTC921.read_text(encoding="utf-8")))
    field = override.overrides["io1148"]
    assert field.fields is not None
    assert set(field.fields) == {"rssi", "rsrp", "sinr", "rsrq"}


def test_ftc921_word_itself_is_not_an_entity() -> None:
    """Its value is meaningless on its own, and it used to be published."""
    profile = parse_profile(json.loads(FTC921.read_text(encoding="utf-8")))
    assert describe("io1148", SAMPLE, profile.overrides["io1148"]) is None
