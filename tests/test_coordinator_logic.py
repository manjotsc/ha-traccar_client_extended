"""Tests for the coordinator's pure decision logic.

These exercise the real methods without standing up Home Assistant, by building
the coordinator without running ``__init__`` and setting only the fields the
methods under test read. Entity-level behaviour needs the HA test harness and
runs in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.traccar_client_extended.coordinator import (
    TraccarClientExtendedCoordinator,
)


def make_coordinator(**overrides):
    """Build a coordinator with only the attributes the pure methods need."""
    coordinator = object.__new__(TraccarClientExtendedCoordinator)
    coordinator.max_accuracy = 0.0
    coordinator.skip_accuracy_filter_for = []
    coordinator.discover_unknown = True
    coordinator.max_discovered = 40
    coordinator.attribute_overrides = {}
    coordinator.ignored_attributes = frozenset()
    coordinator.profiles = {}
    coordinator.auto_add_devices = True
    coordinator.config_entry = SimpleNamespace(subentries={})
    coordinator.data = {}
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


# -- Accuracy filter ---------------------------------------------------------


def test_accuracy_filter_is_off_by_default() -> None:
    """A zero threshold means every position is accepted."""
    coordinator = make_coordinator(max_accuracy=0.0)
    assert coordinator._accuracy_ok({}, {"accuracy": 5000})


def test_accuracy_filter_rejects_imprecise_positions() -> None:
    """This is the setting core declares but never wires up in the webhook."""
    coordinator = make_coordinator(max_accuracy=100.0)
    assert coordinator._accuracy_ok({}, {"accuracy": 50})
    assert not coordinator._accuracy_ok({}, {"accuracy": 150})


def test_accuracy_filter_accepts_the_boundary() -> None:
    """A fix exactly at the threshold is good enough."""
    coordinator = make_coordinator(max_accuracy=100.0)
    assert coordinator._accuracy_ok({}, {"accuracy": 100})


def test_missing_accuracy_is_treated_as_zero() -> None:
    """Traccar sends accuracy 0 for GPS fixes and omits it for some devices."""
    coordinator = make_coordinator(max_accuracy=100.0)
    assert coordinator._accuracy_ok({}, {})
    assert coordinator._accuracy_ok({}, {"accuracy": None})


def test_skip_list_exempts_a_position() -> None:
    """Naming an attribute exempts positions that carry it."""
    coordinator = make_coordinator(
        max_accuracy=100.0, skip_accuracy_filter_for=["indoor"]
    )
    position = {"accuracy": 5000, "attributes": {"indoor": True}}
    assert coordinator._accuracy_ok({}, position)


def test_skip_list_also_matches_device_attributes() -> None:
    """Core checks both sides, so match that."""
    coordinator = make_coordinator(
        max_accuracy=100.0, skip_accuracy_filter_for=["indoor"]
    )
    device = {"attributes": {"indoor": True}}
    assert coordinator._accuracy_ok(device, {"accuracy": 5000})


# -- Discovery ---------------------------------------------------------------


def _with_attributes(attributes: dict) -> dict:
    return {
        1: {"device": {}, "position": {}, "geofences": [], "attributes": attributes}
    }


def test_denylisted_attributes_are_never_discoverable() -> None:
    """They would only bloat the recorder."""
    coordinator = make_coordinator(data=_with_attributes({"raw": "ff", "sat": 9}))
    assert set(coordinator.discoverable_attributes(1)) == {"sat"}


def test_unknown_attributes_are_capped() -> None:
    """A chatty tracker must not spawn unbounded entities."""
    unknown = {f"vendorKey{i}": i for i in range(10)}
    coordinator = make_coordinator(max_discovered=3, data=_with_attributes(unknown))
    assert len(coordinator.discoverable_attributes(1)) == 3


def test_curated_attributes_ignore_the_cap() -> None:
    """The cap exists to bound unknown keys, not to hide known ones."""
    attributes = {"batteryLevel": 80, "ignition": True, "totalDistance": 1000}
    attributes.update({f"vendorKey{i}": i for i in range(10)})
    coordinator = make_coordinator(max_discovered=0, data=_with_attributes(attributes))

    discovered = coordinator.discoverable_attributes(1)
    assert {"batteryLevel", "ignition", "totalDistance"} <= set(discovered)
    assert not any(key.startswith("vendorKey") for key in discovered)


def test_numbered_families_do_not_consume_the_budget() -> None:
    """temp1..temp8 are curated in shape even though not listed by name."""
    attributes = {f"temp{i}": float(i) for i in range(1, 9)}
    coordinator = make_coordinator(max_discovered=0, data=_with_attributes(attributes))
    assert len(coordinator.discoverable_attributes(1)) == 8


def test_discovery_can_be_disabled() -> None:
    """With discovery off, only curated attributes become entities."""
    attributes = {"batteryLevel": 80, "vendorKey": 1}
    coordinator = make_coordinator(
        discover_unknown=False, data=_with_attributes(attributes)
    )
    assert set(coordinator.discoverable_attributes(1)) == {"batteryLevel"}


def test_unknown_device_yields_nothing() -> None:
    """Called for a device that has gone away, this must not raise."""
    coordinator = make_coordinator(data={})
    assert coordinator.discoverable_attributes(99) == {}


# -- Ignoring attributes from the picker --------------------------------------


def test_ignored_attributes_produce_no_entity() -> None:
    """Unticking an attribute is the whole point of the picker."""
    coordinator = make_coordinator(ignored_attributes=frozenset({"io800"}))

    assert coordinator.overrides_for(None)["io800"].platform == "ignore"


def test_a_profile_outranks_the_ignore_picker() -> None:
    """The picker is a server-wide setting, so it is the least specific one.

    It was applied last for a while, which made the bluntest control in the
    integration beat both a profile describing the hardware and a device
    override naming the attribute outright -- silently, and against the
    layering the rest of the integration documents and follows.
    """
    from custom_components.traccar_client_extended.attributes import AttributeOverride
    from custom_components.traccar_client_extended.profiles import DeviceProfile

    profile = DeviceProfile(
        name="p",
        display_name="P",
        models=("m",),
        overrides={"io800": AttributeOverride(platform="sensor", unit="V")},
    )
    coordinator = make_coordinator(ignored_attributes=frozenset({"io800"}))
    coordinator.profile_for = lambda _unique_id: profile

    assert coordinator.overrides_for(None)["io800"].platform == "sensor"


def test_a_device_override_outranks_the_ignore_picker() -> None:
    """Ignoring everywhere except one tracker has to be expressible.

    That is the whole point of a fleet-wide default with per-device exceptions,
    and applying the picker last forbade it by accident.
    """
    from types import SimpleNamespace

    coordinator = make_coordinator(ignored_attributes=frozenset({"io800"}))
    coordinator.subentry_for = lambda _unique_id: SimpleNamespace(
        data={"attribute_overrides": [{"attribute": "io800", "treat_as": "sensor"}]}
    )

    assert coordinator.overrides_for("van")["io800"].platform == "sensor"


def test_ignored_attributes_are_not_discovered() -> None:
    """Otherwise the entity is created and then has no description to render."""
    coordinator = make_coordinator(
        ignored_attributes=frozenset({"io800"}),
        data={
            1: {
                "device": {"uniqueId": "van"},
                "attributes": {"io800": 14583, "batteryLevel": 90},
            }
        },
    )

    assert "io800" not in coordinator.discoverable_attributes(1)
    assert "batteryLevel" in coordinator.discoverable_attributes(1)


def test_the_picker_offers_what_the_fleet_reports() -> None:
    """A fixed list cannot work: raw ioNNN keys are unknowable in advance."""
    coordinator = make_coordinator(
        data={
            1: {"attributes": {"io800": 1, "batteryLevel": 90}},
            2: {"attributes": {"ignition": True, "raw": "deadbeef"}},
        }
    )

    keys = coordinator.seen_attribute_keys()

    assert keys == ["batteryLevel", "ignition", "io800"]
    # `raw` is denylisted, so it never becomes an entity and offering to turn
    # it off would imply it was on.
    assert "raw" not in keys
