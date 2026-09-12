"""Tests for automatic profile matching by device model.

A fleet of identical trackers should be configured once. Profiles already
declare which models they cover, so a device whose Traccar model matches gets
the profile without anyone selecting it per device.
"""

from __future__ import annotations

from types import MappingProxyType

from custom_components.traccar_client_extended.const import (
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_UNIQUE_ID,
    PROFILE_AUTO,
    PROFILE_NONE,
    SUBENTRY_TYPE_DEVICE,
)
from custom_components.traccar_client_extended.profiles import DeviceProfile, match
from homeassistant.config_entries import ConfigSubentry

from .test_coordinator_logic import make_coordinator
from .test_subentries import _Entry


def profile(name: str, models=(), **kw) -> DeviceProfile:
    return DeviceProfile(
        name=name, display_name=name.title(), models=tuple(models), **kw
    )


# -- match() ------------------------------------------------------------------


def test_exact_model_match() -> None:
    profiles = {"a": profile("a", models=("FTC921",)), "b": profile("b")}
    assert match(profiles, "FTC921").name == "a"


def test_match_is_case_and_space_insensitive() -> None:
    """Traccar's model field is free text a person typed."""
    profiles = {"a": profile("a", models=("FTC921",))}
    assert match(profiles, " ftc921 ").name == "a"


def test_no_model_matches_nothing() -> None:
    profiles = {"a": profile("a", models=("FTC921",))}
    assert match(profiles, None) is None
    assert match(profiles, "  ") is None


def test_unknown_model_matches_nothing() -> None:
    profiles = {"a": profile("a", models=("FTC921",))}
    assert match(profiles, "FMB920") is None


def test_partial_model_does_not_match() -> None:
    """Substring matching would map one model's IO parameters onto another's."""
    profiles = {"a": profile("a", models=("FTC921",))}
    assert match(profiles, "FTC9") is None
    assert match(profiles, "FTC921X") is None


def test_ambiguous_model_matches_nothing() -> None:
    """Two profiles claiming a model is a decision only the user can make."""
    profiles = {
        "a": profile("a", models=("FTC921",)),
        "b": profile("b", models=("FTC921",)),
    }
    assert match(profiles, "FTC921") is None


def test_protocol_alone_never_auto_applies() -> None:
    """Every Teltonika tracker speaks 'teltonika'; the models differ wildly."""
    profiles = {"a": profile("a", protocols=("teltonika",))}
    assert match(profiles, "teltonika") is None


# -- Resolution on a device ---------------------------------------------------


def device(unique_id: str, model: str | None) -> dict:
    return {
        "device": {"uniqueId": unique_id, "model": model},
        "position": {},
        "geofences": [],
        "attributes": {},
    }


def coordinator_with(profiles, devices, *subentries):
    c = make_coordinator(profiles=profiles, data=dict(enumerate(devices)))
    c.config_entry = _Entry(subentries)
    return c


def subentry(unique_id: str, choice: str) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(
            {CONF_DEVICE_UNIQUE_ID: unique_id, CONF_DEVICE_PROFILE: choice}
        ),
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=unique_id,
        unique_id=unique_id,
    )


def test_every_device_of_a_model_gets_the_profile() -> None:
    """The point of the feature: configure the fleet once, not per device."""
    profiles = {"ftc": profile("ftc", models=("FTC921",))}
    c = coordinator_with(
        profiles,
        [device("a", "FTC921"), device("b", "FTC921"), device("c", "FMB920")],
    )
    assert c.profile_for("a").name == "ftc"
    assert c.profile_for("b").name == "ftc"
    assert c.profile_for("c") is None


def test_auto_applies_without_a_subentry_choice() -> None:
    profiles = {"ftc": profile("ftc", models=("FTC921",))}
    c = coordinator_with(profiles, [device("a", "FTC921")], subentry("a", PROFILE_AUTO))
    assert c.profile_for("a").name == "ftc"


def test_explicit_none_disables_matching() -> None:
    """A user must be able to opt a device out of an otherwise correct match."""
    profiles = {"ftc": profile("ftc", models=("FTC921",))}
    c = coordinator_with(profiles, [device("a", "FTC921")], subentry("a", PROFILE_NONE))
    assert c.profile_for("a") is None


def test_explicit_choice_overrides_the_match() -> None:
    profiles = {
        "ftc": profile("ftc", models=("FTC921",)),
        "other": profile("other"),
    }
    c = coordinator_with(profiles, [device("a", "FTC921")], subentry("a", "other"))
    assert c.profile_for("a").name == "other"


def test_missing_chosen_profile_is_not_fatal() -> None:
    """A profile can be removed from the library after being selected."""
    c = coordinator_with({}, [device("a", "FTC921")], subentry("a", "deleted_profile"))
    assert c.profile_for("a") is None


def test_overrides_include_the_matched_profile() -> None:
    """Auto-matching has to reach the attribute mapping, not just the lookup."""
    from custom_components.traccar_client_extended.attributes import AttributeOverride

    profiles = {
        "ftc": DeviceProfile(
            name="ftc",
            display_name="FTC",
            models=("FTC921",),
            overrides={
                "io800": AttributeOverride(
                    platform="sensor", device_class="voltage", unit="V", scale=0.001
                )
            },
        )
    }
    c = coordinator_with(profiles, [device("a", "FTC921")])
    resolved = c.overrides_for("a")
    assert resolved["io800"].device_class == "voltage"
    assert resolved["io800"].scale == 0.001
