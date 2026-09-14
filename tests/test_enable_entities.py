"""Tests for the one-shot "switch on this device's disabled entities" action.

Home Assistant owns the enabled state once an entity exists --
``entity_registry_enabled_default`` only decides what a *new* entity starts as
-- so this writes to the registry directly. That makes it an action rather than
a setting, and these tests are mostly about what it must *not* touch.

The registry is stubbed the way `test_prune.py` stubs it, rather than built on
a real `hass`: the pinned pytest-homeassistant-custom-component needs Python
3.14.2 and this suite has to stay runnable without it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.traccar_client_extended.config_flow as config_flow
import custom_components.traccar_client_extended.entity_defaults as entity_defaults
from custom_components.traccar_client_extended.config_flow import (
    enable_subentry_entities,
)
from custom_components.traccar_client_extended.entity_defaults import (
    reset_subentry_entities,
)
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

HASS = SimpleNamespace()


@pytest.fixture
def registry(monkeypatch):
    """Stand in for the entity registry, recording what gets re-enabled."""
    entities: list[SimpleNamespace] = []
    updated: dict[str, object] = {}

    def async_update_entity(entity_id, **changes):
        updated[entity_id] = changes["disabled_by"]
        for entity in entities:
            if entity.entity_id == entity_id:
                entity.disabled_by = changes["disabled_by"]

    fake = SimpleNamespace(async_update_entity=async_update_entity)
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: fake)
    monkeypatch.setattr(
        config_flow.er,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: list(entities),
    )
    return SimpleNamespace(entities=entities, updated=updated)


def register(
    registry, subentry_id: str, name: str, disabled_by, device: str = "cobalt"
) -> str:
    """Add one registry entry for a device inside a model sub-entry."""
    entity_id = f"sensor.{device}_{name}"
    registry.entities.append(
        SimpleNamespace(
            entity_id=entity_id,
            unique_id=f"{device}_attr_{name}",
            config_subentry_id=subentry_id,
            disabled_by=disabled_by,
        )
    )
    return entity_id


def test_enables_what_the_integration_switched_off(registry) -> None:
    """The diagnostics created off by default are the point of the action."""
    entity_id = register(registry, "ftc921", "rssi", RegistryEntryDisabler.INTEGRATION)

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 1
    assert registry.updated == {entity_id: None}


def test_enables_what_the_user_switched_off(registry) -> None:
    """Enable all means all: the user has just asked for them."""
    entity_id = register(registry, "ftc921", "sat", RegistryEntryDisabler.USER)

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 1
    assert registry.updated == {entity_id: None}


def test_leaves_the_other_model_alone(registry) -> None:
    """Scoped to the sub-entry as well as the device."""
    mine = register(registry, "ftc921", "rssi", RegistryEntryDisabler.INTEGRATION)
    register(registry, "ftc305", "rssi", RegistryEntryDisabler.INTEGRATION, "kestrel")

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 1
    assert registry.updated == {mine: None}


def test_leaves_the_other_devices_of_the_model_alone(registry) -> None:
    """The regression: a sub-entry is a whole fleet now.

    Filtering on the sub-entry alone switched on every FTC921 in the place,
    from a form headed with one van's name.
    """
    mine = register(registry, "ftc921", "rssi", RegistryEntryDisabler.INTEGRATION)
    register(registry, "ftc921", "rssi", RegistryEntryDisabler.INTEGRATION, "malibu")

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 1
    assert registry.updated == {mine: None}


@pytest.mark.parametrize(
    "disabler",
    [
        RegistryEntryDisabler.CONFIG_ENTRY,
        RegistryEntryDisabler.DEVICE,
        RegistryEntryDisabler.HASS,
    ],
)
def test_leaves_larger_disablements_alone(registry, disabler) -> None:
    """These mean something bigger is off; clearing them would not even work."""
    register(registry, "ftc921", "rssi", disabler)

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 0
    assert registry.updated == {}


def test_already_enabled_entities_are_left_untouched(registry) -> None:
    """No write, so no pointless registry churn and no reload storm."""
    register(registry, "ftc921", "rssi", None)

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 0
    assert registry.updated == {}


def test_nothing_to_do_is_not_an_error(registry) -> None:
    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 0


def test_enables_every_disabled_entity_of_the_device(registry) -> None:
    first = register(registry, "ftc921", "rssi", RegistryEntryDisabler.INTEGRATION)
    second = register(registry, "ftc921", "hdop", RegistryEntryDisabler.INTEGRATION)
    third = register(registry, "ftc921", "sat", RegistryEntryDisabler.USER)

    assert enable_subentry_entities(HASS, "entry", "ftc921", "cobalt") == 3
    assert registry.updated == {first: None, second: None, third: None}


# -- Reset to defaults --------------------------------------------------------


@pytest.fixture
def reset_registry(monkeypatch):
    """The same stub, patched into the module the reset lives in."""
    entities: list[SimpleNamespace] = []
    updated: dict[str, object] = {}

    def async_update_entity(entity_id, **changes):
        updated[entity_id] = changes["disabled_by"]

    fake = SimpleNamespace(async_update_entity=async_update_entity)
    monkeypatch.setattr(entity_defaults.er, "async_get", lambda _hass: fake)
    monkeypatch.setattr(
        entity_defaults.er,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: list(entities),
    )
    return SimpleNamespace(entities=entities, updated=updated)


def registered(reset_registry, unique_id: str, disabled_by) -> str:
    entity_id = f"sensor.{unique_id}"
    reset_registry.entities.append(
        SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            config_subentry_id="sub",
            disabled_by=disabled_by,
        )
    )
    return entity_id


def with_defaults(monkeypatch, defaults: dict[str, bool]) -> None:
    monkeypatch.setattr(
        entity_defaults, "enabled_defaults", lambda _coordinator, _unique: defaults
    )


def do_reset() -> int:
    return reset_subentry_entities(HASS, object(), "entry", "sub", "cobalt")


def test_reset_switches_a_diagnostic_back_off(reset_registry, monkeypatch) -> None:
    """The undo for "enable all": defaults win again."""
    entity_id = registered(reset_registry, "cobalt_attr_rssi", None)
    with_defaults(monkeypatch, {"cobalt_attr_rssi": False})

    assert do_reset() == 1
    assert reset_registry.updated == {entity_id: RegistryEntryDisabler.INTEGRATION}


def test_reset_switches_a_default_on_entity_back_on(
    reset_registry, monkeypatch
) -> None:
    entity_id = registered(
        reset_registry, "cobalt_attr_ignition", RegistryEntryDisabler.USER
    )
    with_defaults(monkeypatch, {"cobalt_attr_ignition": True})

    assert do_reset() == 1
    assert reset_registry.updated == {entity_id: None}


def test_reset_writes_nothing_when_already_correct(reset_registry, monkeypatch) -> None:
    registered(reset_registry, "cobalt_attr_ignition", None)
    registered(reset_registry, "cobalt_attr_rssi", RegistryEntryDisabler.INTEGRATION)
    with_defaults(
        monkeypatch, {"cobalt_attr_ignition": True, "cobalt_attr_rssi": False}
    )

    assert do_reset() == 0
    assert reset_registry.updated == {}


def test_reset_leaves_an_entity_with_no_description_alone(
    reset_registry, monkeypatch
) -> None:
    """The device stopped reporting the attribute; that is not our call to make."""
    registered(reset_registry, "cobalt_attr_io999", RegistryEntryDisabler.USER)
    with_defaults(monkeypatch, {"cobalt_attr_rssi": False})

    assert do_reset() == 0
    assert reset_registry.updated == {}


def test_reset_leaves_larger_disablements_alone(reset_registry, monkeypatch) -> None:
    registered(reset_registry, "cobalt_attr_ignition", RegistryEntryDisabler.DEVICE)
    with_defaults(monkeypatch, {"cobalt_attr_ignition": True})

    assert do_reset() == 0
    assert reset_registry.updated == {}


def test_reset_ignores_other_devices(reset_registry, monkeypatch) -> None:
    reset_registry.entities.append(
        SimpleNamespace(
            entity_id="sensor.kestrel_attr_rssi",
            unique_id="kestrel_attr_rssi",
            config_subentry_id="other",
            disabled_by=None,
        )
    )
    with_defaults(monkeypatch, {"kestrel_attr_rssi": False})

    assert do_reset() == 0
    assert reset_registry.updated == {}


def test_reset_does_nothing_for_an_unknown_device(reset_registry, monkeypatch) -> None:
    """No descriptions means no opinion, not "switch everything off"."""
    registered(reset_registry, "cobalt_attr_rssi", None)
    with_defaults(monkeypatch, {})

    assert do_reset() == 0
    assert reset_registry.updated == {}
