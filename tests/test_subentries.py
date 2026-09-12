"""Tests for per-device subentry configuration.

A subentry per device is what lets a mixed fleet work: `io800` means different
things on different Teltonika models, and a phone needs an accuracy filter where
a hardwired tracker does not.
"""

from __future__ import annotations

from types import MappingProxyType

from custom_components.traccar_client_extended.attributes import parse_overrides
from custom_components.traccar_client_extended.const import (
    CONF_ATTRIBUTE_OVERRIDES,
    CONF_DEVICE_UNIQUE_ID,
    CONF_MAX_ACCURACY,
    SUBENTRY_TYPE_DEVICE,
)
from homeassistant.config_entries import ConfigSubentry

from .test_coordinator_logic import make_coordinator


def subentry(unique_id: str, **data) -> ConfigSubentry:
    """Build a device subentry the way the flow would."""
    return ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_UNIQUE_ID: unique_id, **data}),
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=unique_id,
        unique_id=unique_id,
    )


class _Entry:
    """Stands in for the config entry the coordinator reads subentries from."""

    def __init__(self, subentries):
        self.subentries = {s.subentry_id: s for s in subentries}


def with_subentries(*subentries: ConfigSubentry, **overrides):
    """A coordinator reading its subentries from a stub config entry."""
    coordinator = make_coordinator(**overrides)
    coordinator.config_entry = _Entry(subentries)
    return coordinator


def test_device_without_a_subentry_produces_no_entities() -> None:
    """This is what makes the device list opt-in."""
    coordinator = with_subentries()
    assert coordinator.subentry_id_for("phone-1") is None


def test_subentry_id_is_returned_for_a_tracked_device() -> None:
    """Entities are added under the device's own subentry."""
    entry = subentry("phone-1")
    coordinator = with_subentries(entry)
    assert coordinator.subentry_id_for("phone-1") == entry.subentry_id


def test_overrides_fall_back_to_the_server_wide_setting() -> None:
    """A device with no overrides of its own still gets the entry's."""
    coordinator = with_subentries(
        subentry("phone-1"),
        attribute_overrides=parse_overrides("io800: voltage, V, x0.001"),
    )
    resolved = coordinator.overrides_for("phone-1")
    assert resolved["io800"].device_class == "voltage"


def test_device_overrides_win_over_the_server_wide_ones() -> None:
    """One tracker can reinterpret a key the rest of the fleet reads differently."""
    coordinator = with_subentries(
        subentry("obd-2", **{CONF_ATTRIBUTE_OVERRIDES: "io800: temperature, °C, x0.1"}),
        attribute_overrides=parse_overrides("io800: voltage, V, x0.001"),
    )
    resolved = coordinator.overrides_for("obd-2")
    assert resolved["io800"].device_class == "temperature"
    assert resolved["io800"].scale == 0.1


def test_server_wide_overrides_survive_alongside_device_ones() -> None:
    """Layering merges rather than replaces."""
    coordinator = with_subentries(
        subentry("obd-2", **{CONF_ATTRIBUTE_OVERRIDES: "io801: humidity, %"}),
        attribute_overrides=parse_overrides("io800: voltage, V, x0.001"),
    )
    resolved = coordinator.overrides_for("obd-2")
    assert set(resolved) == {"io800", "io801"}


def test_unknown_device_gets_only_the_server_wide_overrides() -> None:
    """Called before a subentry exists, this must not raise."""
    coordinator = with_subentries(
        attribute_overrides=parse_overrides("io800: voltage, V, x0.001")
    )
    assert set(coordinator.overrides_for("nobody")) == {"io800"}


def test_accuracy_falls_back_to_the_entry_setting() -> None:
    """Most devices will not set their own threshold."""
    coordinator = with_subentries(subentry("phone-1"), max_accuracy=100.0)
    assert coordinator.max_accuracy_for("phone-1") == 100.0


def test_device_accuracy_overrides_the_entry_setting() -> None:
    """A phone needs filtering where a hardwired tracker does not."""
    coordinator = with_subentries(
        subentry("phone-1", **{CONF_MAX_ACCURACY: 50.0}),
        subentry("obd-2", **{CONF_MAX_ACCURACY: 0.0}),
        max_accuracy=100.0,
    )
    assert coordinator.max_accuracy_for("phone-1") == 50.0
    assert coordinator.max_accuracy_for("obd-2") == 0.0


def test_accuracy_filter_uses_the_device_threshold() -> None:
    """The filter reads the per-device value, not just the entry's."""
    coordinator = with_subentries(
        subentry("phone-1", **{CONF_MAX_ACCURACY: 50.0}), max_accuracy=0.0
    )
    device = {"uniqueId": "phone-1"}
    assert coordinator._accuracy_ok(device, {"accuracy": 20})
    # The entry-level filter is off, but this device asked for one.
    assert not coordinator._accuracy_ok(device, {"accuracy": 80})


def test_device_can_disable_a_server_wide_filter() -> None:
    """Setting zero on a device turns the filter off for it alone."""
    coordinator = with_subentries(
        subentry("obd-2", **{CONF_MAX_ACCURACY: 0.0}), max_accuracy=50.0
    )
    assert coordinator._accuracy_ok({"uniqueId": "obd-2"}, {"accuracy": 5000})
    # A device without its own setting still uses the server-wide one.
    assert not coordinator._accuracy_ok({"uniqueId": "other"}, {"accuracy": 5000})


def test_edited_subentry_is_seen_without_a_restart() -> None:
    """Regression: the lookup used to be cached at startup.

    Applying a profile replaces the ConfigSubentry object, so a snapshot taken
    when the coordinator was built kept serving the device's old settings and
    the profile appeared to do nothing.
    """
    original = subentry("phone-1")
    coordinator = with_subentries(original)
    assert coordinator.overrides_for("phone-1") == {}

    edited = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_UNIQUE_ID: "phone-1",
                CONF_ATTRIBUTE_OVERRIDES: "io800: voltage, V, x0.001",
            }
        ),
        subentry_id=original.subentry_id,
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=original.title,
        unique_id=original.unique_id,
    )
    coordinator.config_entry.subentries[original.subentry_id] = edited

    resolved = coordinator.overrides_for("phone-1")
    assert resolved["io800"].device_class == "voltage"
    assert resolved["io800"].scale == 0.001
