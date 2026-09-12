"""Tests for device profiles, including validation of the shipped library.

The library is meant to grow by pull request from people who own hardware we
do not. These tests are what makes accepting those safe: a profile that pairs a
device class with an invalid unit produces a sensor that looks fine and silently
never records long-term statistics, so it has to fail CI rather than ship.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.traccar_client_extended.profiles import (
    PROFILES_DIRECTORY,
    DeviceProfile,
    ProfileError,
    load_profiles,
    parse_profile,
    suggest,
    validate_units,
)

PROFILES_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "traccar_client_extended"
    / PROFILES_DIRECTORY
)


def profile_files() -> list[Path]:
    """Every shipped profile, excluding underscore-prefixed documentation."""
    return [
        p for p in sorted(PROFILES_PATH.glob("*.json")) if not p.name.startswith("_")
    ]


# -- The shipped library ------------------------------------------------------


def test_library_is_not_empty() -> None:
    """A dropdown with nothing in it would be a bug, not a state to allow."""
    assert profile_files()


@pytest.mark.parametrize("path", profile_files(), ids=lambda p: p.name)
def test_shipped_profile_is_valid(path: Path) -> None:
    """Every shipped profile parses and has valid device class/unit pairs."""
    parse_profile(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", profile_files(), ids=lambda p: p.name)
def test_filename_matches_profile_name(path: Path) -> None:
    """Keeping these in step is what makes the library browsable."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["name"] == path.stem


@pytest.mark.parametrize("path", profile_files(), ids=lambda p: p.name)
def test_profile_has_a_display_name_distinct_from_its_slug(path: Path) -> None:
    """`name` is the machine slug; `display_name` is what a person reads."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["display_name"]
    assert raw["display_name"] != raw["name"]


def test_profile_names_are_unique() -> None:
    """Two profiles claiming one slug would silently shadow each other."""
    names = [json.loads(p.read_text(encoding="utf-8"))["name"] for p in profile_files()]
    assert len(names) == len(set(names))


def test_library_loads() -> None:
    """The real loader reads the real directory."""
    profiles = load_profiles(PROFILES_PATH)
    assert profiles
    assert all(isinstance(p, DeviceProfile) for p in profiles.values())


def test_template_is_not_loaded_as_a_profile() -> None:
    """Underscore-prefixed files are documentation."""
    assert (PROFILES_PATH / "_TEMPLATE.json").exists()
    assert "vendor_model" not in load_profiles(PROFILES_PATH)


# -- Unit validation ----------------------------------------------------------


def test_valid_pairing_passes() -> None:
    validate_units("voltage", "V")
    validate_units("temperature", "°C")


def test_invalid_pairing_is_rejected() -> None:
    """The failure this whole test module exists to prevent."""
    with pytest.raises(ProfileError, match="does not accept unit"):
        validate_units("voltage", "°C")


def test_no_device_class_is_always_fine() -> None:
    validate_units(None, "widgets")


def test_unknown_device_class_is_left_alone() -> None:
    """It may be a binary sensor class, which is checked separately."""
    validate_units("motion", None)


# -- Parsing ------------------------------------------------------------------


def minimal(**overrides) -> dict:
    base = {
        "name": "vendor_model",
        "display_name": "Vendor Model",
        "attributes": {"io1": {"device_class": "voltage", "unit": "V", "scale": 0.001}},
    }
    base.update(overrides)
    return base


def test_parse_minimal_profile() -> None:
    profile = parse_profile(minimal())
    assert profile.name == "vendor_model"
    assert profile.overrides["io1"].scale == 0.001


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({}, "needs a 'name'"),
        ({"name": "x"}, "needs a 'display_name'"),
        ({"name": "x", "display_name": "X"}, "non-empty 'attributes'"),
        (
            minimal(attributes={"io1": {"device_class": "voltage", "unit": "°C"}}),
            "does not accept unit",
        ),
        (minimal(attributes={"io1": {"platform": "nonsense"}}), "unknown platform"),
        (minimal(attributes={"io1": {"scale": "fast"}}), "non-numeric scale"),
        (minimal(attributes={"io1": {"precision": 1.5}}), "non-integer precision"),
        (minimal(attributes={"io1": "not an object"}), "must be an object"),
    ],
)
def test_invalid_profiles_are_rejected(payload: dict, match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(payload)


def test_ignore_platform_is_allowed() -> None:
    """Profiles curate away attributes that would only bloat the recorder."""
    profile = parse_profile(minimal(attributes={"io1": {"platform": "ignore"}}))
    assert profile.overrides["io1"].platform == "ignore"


def test_bad_file_does_not_break_the_library(tmp_path: Path) -> None:
    """One malformed contribution must not take the integration down."""
    (tmp_path / "good.json").write_text(
        json.dumps(minimal(name="good")), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "invalid.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert set(load_profiles(tmp_path)) == {"good"}


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_profiles(tmp_path / "nope") == {}


# -- Suggestions --------------------------------------------------------------


def make(name: str, **kw) -> DeviceProfile:
    return DeviceProfile(name=name, display_name=name.title(), **kw)


def test_model_match_ranks_first() -> None:
    """The strongest signal Traccar gives us."""
    profiles = {
        "other": make("other"),
        "match": make("match", models=("FTC921",), protocols=("teltonika",)),
    }
    assert suggest(profiles, protocol=None, model="FTC921")[0].name == "match"


def test_protocol_match_ranks_above_unrelated() -> None:
    profiles = {"other": make("other"), "telt": make("telt", protocols=("teltonika",))}
    assert suggest(profiles, protocol="teltonika", model=None)[0].name == "telt"


def test_vendor_in_model_text_counts() -> None:
    """Traccar's model field is free text, so it is often just a brand name."""
    profiles = {"other": make("other"), "telt": make("telt", vendor="Teltonika")}
    assert suggest(profiles, protocol=None, model="Teltonika tracker")[0].name == "telt"


def test_suggest_returns_everything() -> None:
    """Ranking must never hide a profile the user might want."""
    profiles = {"a": make("a"), "b": make("b", protocols=("teltonika",))}
    assert len(suggest(profiles, "teltonika", None)) == 2


def test_suggest_without_hints_is_alphabetical() -> None:
    profiles = {"z": make("zebra"), "a": make("alpha")}
    assert [p.name for p in suggest(profiles)] == ["alpha", "zebra"]
