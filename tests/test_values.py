"""Tests for enumerated readings: a profile's raw-value-to-label map.

Teltonika's "Network Type" (AVL ID 237) is a mode byte, not a measurement. The
profile schema could describe its name and its unit but had no way to say what
0 or 1 meant, so those meanings lived in prose in a `description` field that
nothing reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.traccar_client_extended.attributes import (
    AttributeOverride,
    decode_text,
    describe,
    describe_fields,
    value_label,
)
from custom_components.traccar_client_extended.profiles import (
    PROFILES_DIRECTORY,
    DeviceProfile,
    ProfileError,
    _parse_attribute,
    parse_profile,
)

LABELS = {"0": "3G", "1": "GSM", "2": "eMTC"}

PROFILES_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "traccar_client_extended"
    / PROFILES_DIRECTORY
)


def _profile(slug: str) -> DeviceProfile:
    """Parse one shipped profile, the way the loader does.

    Read from disk rather than rebuilt in the test: what these assert is that
    the JSON people actually install says what it is meant to say.
    """
    raw = json.loads((PROFILES_PATH / f"{slug}.json").read_text(encoding="utf-8"))
    return parse_profile(raw)


# -- Lookup -------------------------------------------------------------------


def test_labels_a_known_value() -> None:
    assert value_label(LABELS, 1) == "GSM"
    assert value_label(LABELS, "2") == "eMTC"


def test_float_finds_the_integer_label() -> None:
    """Traccar flips an attribute between int and float across reports."""
    assert value_label(LABELS, 2.0) == "eMTC"


def test_unlabelled_value_returns_none() -> None:
    """The caller falls back to the raw number rather than showing Unknown."""
    assert value_label(LABELS, 7) is None
    assert value_label(LABELS, 2.5) is None
    assert value_label(LABELS, None) is None
    assert value_label(LABELS, True) is None
    assert value_label(None, 1) is None
    assert value_label({}, 1) is None


# -- The description it produces ----------------------------------------------


def test_description_carries_the_labels() -> None:
    override = AttributeOverride(platform="sensor", values=LABELS)
    description = describe("io237", 1, override)
    assert description.value_labels == LABELS


def test_labelled_sensor_is_not_given_a_state_class() -> None:
    """A text state has nothing to record as a statistic."""
    override = AttributeOverride(platform="sensor", values=LABELS)
    assert describe("io237", 1, override).state_class is None


# -- State class --------------------------------------------------------------


def test_explicit_state_class_survives() -> None:
    """A bar count has no unit to infer one from, and still keeps history."""
    override = _parse_attribute("rssi", {"state_class": "measurement"})
    assert describe("rssi", 3, override).state_class == "measurement"


def test_state_class_is_still_inferred_from_a_unit() -> None:
    override = _parse_attribute("io800", {"unit": "V", "device_class": "voltage"})
    assert describe("io800", 1, override).state_class == "measurement"


def test_unknown_state_class_is_refused() -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("rssi", {"state_class": "averaged"})


def test_state_class_with_values_is_refused() -> None:
    """A labelled state is text; there is nothing to record."""
    with pytest.raises(ProfileError):
        _parse_attribute("io237", {"values": LABELS, "state_class": "measurement"})


# -- Decoding a re-encoded value ----------------------------------------------

#: A real FTC921 reading: Traccar hex-dumped the 22-byte ASCII ICCID.
ICCID_HEX = "3839313033303030303030303331373139333433"


def test_hex_ascii_decodes() -> None:
    assert decode_text("hex_ascii", ICCID_HEX) == "89103000000031719343"


def test_padding_is_trimmed() -> None:
    """Fixed-width fields are NUL- or space-padded depending on the vendor."""
    assert decode_text("hex_ascii", "41424300002020") == "ABC"


@pytest.mark.parametrize(
    "value",
    [
        "zzzz",  # not hex
        "38393",  # odd length
        "",
        "0001",  # decodes to control characters, which are not a reading
    ],
)
def test_undecodable_values_pass_through(value: str) -> None:
    """A failed conversion must never cost the reading."""
    assert decode_text("hex_ascii", value) == value


def test_an_already_plain_value_is_left_alone() -> None:
    """Firmware that sends it as text must not be mangled.

    89103000000031719343 read as hex is not ASCII, so it fails and passes
    through -- which is the behaviour that matters, not the reason.
    """
    assert decode_text("hex_ascii", "89103000000031719343") == "89103000000031719343"


@pytest.mark.parametrize("value", [None, 1234, True, 12.5])
def test_non_strings_pass_through(value: object) -> None:
    assert decode_text("hex_ascii", value) == value


def test_no_decode_is_a_no_op() -> None:
    assert decode_text(None, ICCID_HEX) == ICCID_HEX


def test_decode_reaches_the_description() -> None:
    override = _parse_attribute("io641", {"decode": "hex_ascii"})
    assert describe("io641", ICCID_HEX, override).decode == "hex_ascii"


def test_unknown_decode_is_refused() -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io641", {"decode": "base64"})


@pytest.mark.parametrize(
    "extra",
    [
        {"unit": "V"},
        {"device_class": "voltage", "unit": "V"},
        {"scale": 2},
        {"offset": 1},
        {"values": {"1": "One"}},
        {"fields": {"a": {"byte": 0}}},
        {"platform": "binary_sensor"},
    ],
)
def test_decode_with_numeric_intent_is_refused(extra: dict) -> None:
    """A decoded value is text; none of those apply to it."""
    with pytest.raises(ProfileError):
        _parse_attribute("io641", {"decode": "hex_ascii"} | extra)


# -- Entity category ----------------------------------------------------------


def test_diagnostic_category_is_applied() -> None:
    override = _parse_attribute("rssi", {"entity_category": "diagnostic"})
    assert describe("rssi", 3, override).entity_category == "diagnostic"


def test_diagnostic_category_applies_to_binary_sensors_too() -> None:
    override = _parse_attribute(
        "io303", {"platform": "binary_sensor", "entity_category": "diagnostic"}
    )
    assert describe("io303", True, override).entity_category == "diagnostic"


def test_no_category_by_default() -> None:
    """An override otherwise drops the curated one, which is the old behaviour."""
    assert describe("rssi", 3, _parse_attribute("rssi", {})).entity_category is None


def test_disabled_by_default_is_applied() -> None:
    override = _parse_attribute("rssi", {"enabled_by_default": False})
    assert describe("rssi", 3, override).entity_registry_enabled_default is False


def test_enabled_by_default_unless_said_otherwise() -> None:
    """Discovery exists so an unrecognised reading still shows up."""
    override = _parse_attribute("rssi", {})
    assert describe("rssi", 3, override).entity_registry_enabled_default is True


def test_disabled_by_default_applies_to_binary_sensors_too() -> None:
    override = _parse_attribute(
        "io303", {"platform": "binary_sensor", "enabled_by_default": False}
    )
    assert describe("io303", True, override).entity_registry_enabled_default is False


def test_non_boolean_enabled_by_default_is_refused() -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("rssi", {"enabled_by_default": "no"})


def test_config_category_is_refused() -> None:
    """A sensor carrying it raises on add, so the entity never appears at all."""
    with pytest.raises(ProfileError):
        _parse_attribute("rssi", {"entity_category": "config"})


def test_unknown_category_is_refused() -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("rssi", {"entity_category": "hidden"})


# -- The shipped FTC921 signal entries ----------------------------------------


def test_ftc921_renames_the_bar_count() -> None:
    """Traccar calls AVL 21 `rssi`, but it is 0..5 bars, not dBm."""
    profile = _ftc921()
    override = profile.overrides["rssi"]
    assert override.name == "Signal level"
    assert describe("rssi", 3, override).state_class == "measurement"


@pytest.mark.parametrize("slug", ["teltonika_ftc921", "teltonika_ftc305"])
def test_signal_level_keeps_the_built_in_treatment(slug: str) -> None:
    """Curated `rssi` is diagnostic and off by default; an override inherits
    neither, so the profile restates both."""
    description = describe("rssi", 3, _profile(slug).overrides["rssi"])
    assert description.entity_category == "diagnostic"
    assert description.entity_registry_enabled_default is False


def test_ftc921_decodes_the_iccid() -> None:
    """Traccar hex-dumps AVL 641, so the profile has to undo it."""
    override = _profile("teltonika_ftc921").overrides["io641"]
    description = describe("io641", ICCID_HEX, override)
    assert description.decode == "hex_ascii"
    assert description.entity_category == "diagnostic"
    assert decode_text(description.decode, ICCID_HEX) == "89103000000031719343"


@pytest.mark.parametrize("slug", ["teltonika_ftc921", "teltonika_ftc305"])
def test_packed_signal_fields_are_diagnostic_but_visible(slug: str) -> None:
    """Beside Signal level in the diagnostic section, not hidden behind it."""
    descriptions = describe_fields("io1148", _profile(slug).overrides["io1148"])
    assert len(descriptions) == 4
    for description in descriptions:
        assert description.entity_category == "diagnostic"
        assert description.entity_registry_enabled_default is True


def test_both_teltonika_profiles_share_the_network_type_map() -> None:
    """The FTC305 page documents what the FTC921 page leaves blank."""
    values = _profile("teltonika_ftc921").overrides["io237"].values
    assert values == _profile("teltonika_ftc305").overrides["io237"].values
    assert values is not None
    assert values["2"] == "4G (LTE)"
    # The FMB-family enum calls 2 eMTC. Pinning this is the point of the test.
    assert values["3"] == "LTE Cat-M1"
    assert values["99"] == "Unknown"


def test_ftc305_matches_ftc921_on_the_signal_entries() -> None:
    """Same modem parameters, same treatment."""
    for name in ("io1149", "rssi"):
        assert name in _profile("teltonika_ftc305").overrides
    packed = _profile("teltonika_ftc305").overrides["io1148"]
    assert packed.platform == "ignore"
    assert packed.fields is not None
    assert set(packed.fields) == {"rssi", "rsrp", "sinr", "rsrq"}


def test_ftc921_has_no_io21() -> None:
    """Traccar registers 21 unconditionally, so io21 never arrives."""
    assert "io21" not in _ftc921().overrides


def test_ftc921_band_zero_reads_as_none_available() -> None:
    profile = _ftc921()
    override = profile.overrides["io1149"]
    assert value_label(describe("io1149", 0, override).value_labels, 0) == "No LTE band"
    assert describe("io1149", 4, override).state_class is None


def _ftc921() -> DeviceProfile:
    return _profile("teltonika_ftc921")


# -- Profile validation -------------------------------------------------------


def test_values_parse() -> None:
    override = _parse_attribute("io237", {"name": "Network type", "values": LABELS})
    assert override.values == LABELS


def test_values_are_stripped() -> None:
    override = _parse_attribute("io237", {"values": {" 1 ": " GSM "}})
    assert override.values == {"1": "GSM"}


@pytest.mark.parametrize(
    "raw",
    [
        {"values": []},
        {"values": {}},
        {"values": {"1": ""}},
        {"values": {"1": 5}},
        {"values": {"": "GSM"}},
    ],
)
def test_malformed_values_are_refused(raw: dict) -> None:
    with pytest.raises(ProfileError):
        _parse_attribute("io237", raw)


def test_values_on_a_binary_sensor_are_refused() -> None:
    """A flag has two states and neither of them is text."""
    with pytest.raises(ProfileError):
        _parse_attribute("io237", {"platform": "binary_sensor", "values": LABELS})


@pytest.mark.parametrize(
    "extra",
    [{"device_class": "voltage", "unit": "V"}, {"unit": "V"}, {"scale": 0.1}],
)
def test_values_with_numeric_intent_are_refused(extra: dict) -> None:
    """A labelled state is text; a unit or a scale says it is a measurement."""
    with pytest.raises(ProfileError):
        _parse_attribute("io237", {"values": LABELS} | extra)


# -- MicTrack MT600 -----------------------------------------------------------


@pytest.mark.parametrize("slug", ["mictrack_mt600", "mictrack_mt700"])
def test_the_mictrack_profiles_stay_identical(slug: str) -> None:
    """Traccar has one MictrackProtocolDecoder for the whole MT family and no
    model gating in it, so the two profiles describe the same keys.

    They are separate files only so each names its own hardware in the model
    card. If a future firmware really does diverge, change this test on
    purpose rather than letting them drift by accident.
    """
    mt600 = _profile("mictrack_mt600").overrides
    mt700 = _profile("mictrack_mt700").overrides
    assert set(mt600) == set(mt700)
    assert mt600["event"].values == mt700["event"].values
    assert _profile(slug).overrides["event"].values["5"] == "SOS"


def test_mt600_labels_the_event_code() -> None:
    """A profile override beats the denylist, which is why this can exist.

    `event` is denylisted because other protocols put an unbounded blob there.
    On MicTrack it is a small integer carrying the reason for the report, and a
    model profile is exactly the scope at which that is knowable.
    """
    override = _profile("mictrack_mt600").overrides["event"]
    description = describe("event", 5, override)
    assert value_label(description.value_labels, 5) == "SOS"
    assert description.entity_category == "diagnostic"


def test_mt600_keeps_an_undocumented_event_as_a_number() -> None:
    """Traccar documents six codes; firmware is free to send others."""
    override = _profile("mictrack_mt600").overrides["event"]
    assert value_label(describe("event", 99, override).value_labels, 99) is None


def test_event_stays_denylisted_without_a_profile() -> None:
    assert describe("event", 5, None) is None


def test_mt600_adds_nothing_traccar_already_types() -> None:
    """`sat`, `battery`, `alarm` and `result` are curated and correct already.

    An entry for one would be noise; an `io<id>` entry would be configuration
    that can never match, since MicTrack's protocol is text.
    """
    overrides = _profile("mictrack_mt600").overrides
    assert set(overrides) == {"event", "type"}
    assert not any(key.startswith("io") for key in overrides)
