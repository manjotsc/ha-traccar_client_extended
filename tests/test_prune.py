"""Tests for removing the entities of attributes that have been turned off.

Ignoring an attribute used to leave its entity registered for ever, showing as
unavailable. That was survivable while turning one off meant writing a line of
override DSL; the picker makes it two clicks, so it now reads as the feature
being broken.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.traccar_client_extended.coordinator as coordinator_module
from custom_components.traccar_client_extended.coordinator import (
    TraccarClientExtendedCoordinator,
)

PRUNE = getattr(
    TraccarClientExtendedCoordinator._prune_ignored_entities,
    "__wrapped__",
    TraccarClientExtendedCoordinator._prune_ignored_entities,
)
NOTIFY = getattr(
    TraccarClientExtendedCoordinator._notify_discovery,
    "__wrapped__",
    TraccarClientExtendedCoordinator._notify_discovery,
)


@pytest.fixture
def registry(monkeypatch):
    """Stand in for the entity registry, recording what gets removed."""
    entities: list[SimpleNamespace] = []
    removed: list[str] = []

    fake = SimpleNamespace(async_remove=removed.append)
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: fake)
    monkeypatch.setattr(
        coordinator_module.er,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: list(entities),
    )
    return SimpleNamespace(entities=entities, removed=removed)


def register(registry, *unique_ids: str) -> None:
    registry.entities.extend(
        SimpleNamespace(unique_id=unique_id, entity_id=f"sensor.{unique_id}")
        for unique_id in unique_ids
    )


def build(attributes: dict, **overrides):
    c = object.__new__(TraccarClientExtendedCoordinator)
    c.hass = SimpleNamespace(data={})
    c.config_entry = SimpleNamespace(subentries={}, entry_id="entry")
    c.attribute_overrides = {}
    c.ignored_attributes = frozenset()
    c.profiles = {}
    c.data = {
        1: {
            "device": {"uniqueId": "van", "status": "online"},
            "position": {},
            "geofences": [],
            "attributes": dict(attributes),
        }
    }
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


def test_an_ignored_attribute_loses_its_entity(registry) -> None:
    """Otherwise the setting looks like it did nothing."""
    register(registry, "van_attr_io800", "van_attr_batteryLevel")
    coordinator = build(
        {"io800": 14583, "batteryLevel": 90}, ignored_attributes=frozenset({"io800"})
    )

    PRUNE(coordinator)

    assert registry.removed == ["sensor.van_attr_io800"]


def test_an_attribute_that_stopped_being_reported_is_kept(registry) -> None:
    """Absence is not a decision. A tracker that goes quiet keeps its history.

    Deleting on absence would be the integration deciding a reading is gone,
    which is the one judgement it never makes.
    """
    register(registry, "van_attr_io800")
    coordinator = build({"batteryLevel": 90})

    PRUNE(coordinator)

    assert registry.removed == []


def test_entities_that_are_not_attributes_are_left_alone(registry) -> None:
    """The tracker and the derived freshness sensors share the device prefix."""
    register(registry, "van", "van_event", "van_attr_io800")
    coordinator = build({"io800": 1}, ignored_attributes=frozenset({"io800"}))

    PRUNE(coordinator)

    assert registry.removed == ["sensor.van_attr_io800"]


def test_a_device_override_rescues_the_entity(registry) -> None:
    """Ignoring everywhere except one tracker must not delete it here."""
    register(registry, "van_attr_io800")
    coordinator = build({"io800": 14583}, ignored_attributes=frozenset({"io800"}))
    coordinator.subentry_for = lambda _unique_id: SimpleNamespace(
        data={"attribute_overrides": [{"attribute": "io800", "treat_as": "sensor"}]}
    )

    PRUNE(coordinator)

    assert registry.removed == []


def test_pruning_runs_on_the_first_pass_after_a_reload(registry, monkeypatch) -> None:
    """Saving the option reloads the entry, so no attribute key has changed.

    Pruning only when a key appears or disappears would mean the removal never
    happened for the one action that asks for it.
    """
    monkeypatch.setattr(
        coordinator_module, "async_dispatcher_send", lambda *a, **k: None
    )
    register(registry, "van_attr_io800")
    coordinator = build({"io800": 14583}, ignored_attributes=frozenset({"io800"}))
    coordinator._record_index = None
    coordinator._device_keys = None

    NOTIFY(coordinator)

    assert registry.removed == ["sensor.van_attr_io800"]
