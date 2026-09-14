"""Tests for one sub-entry per model, and the migration onto it.

The integration page renders one card per sub-entry with that sub-entry's
devices nested inside, and ignores `via_device`. A sub-entry per device could
only ever produce one card per device, so grouping by model means the model has
to *be* the sub-entry.

The migration is the part worth testing hardest: removing a sub-entry deletes
its devices and entities, so folding the old shape into the new one has to move
registry entries across rather than let them be recreated.
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest

import custom_components.traccar_client_extended.subentries as subentries_module
from custom_components.traccar_client_extended.const import (
    CONF_ATTRIBUTE_OVERRIDES,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_UNIQUE_ID,
    CONF_DEVICES,
    CONF_MAX_ACCURACY,
    CONF_MODEL,
    CONF_TRACCAR_DEVICE_ID,
    SUBENTRY_TYPE_DEVICE,
)
from custom_components.traccar_client_extended.profiles import DeviceProfile
from custom_components.traccar_client_extended.subentries import (
    NO_MODEL_TITLE,
    async_sync_shape,
    device_entries,
    find_subentry,
    model_key,
    model_title,
    with_device,
    without_device,
)
from homeassistant.config_entries import ConfigSubentry

# -- Identity -----------------------------------------------------------------


def test_one_key_per_model() -> None:
    assert model_key("FTC921") == "model:ftc921"


def test_casing_and_padding_do_not_split_a_fleet() -> None:
    """Profile matching is case-insensitive; grouping has to agree with it."""
    assert model_key(" ftc921 ") == model_key("FTC921")


def test_punctuation_collapses() -> None:
    assert model_key("FMB-920/A") == "model:fmb_920_a"


@pytest.mark.parametrize("model", [None, "", "   "])
def test_devices_with_no_model_share_one_key(model) -> None:
    """Traccar's model is free text most people never fill in."""
    assert model_key(model) == "model:"


@pytest.mark.parametrize("model", [None, ""])
def test_the_no_model_card_is_named(model) -> None:
    """A card titled "" would read as a bug."""
    assert model_title(model, {}) == NO_MODEL_TITLE


def test_a_profile_names_the_card() -> None:
    profiles = {
        "p": DeviceProfile(
            name="p", display_name="Teltonika FTC921", models=("FTC921",)
        )
    }
    assert model_title("FTC921", profiles) == "Teltonika FTC921"


def test_an_unprofiled_model_keeps_traccar_wording() -> None:
    assert model_title("SomeTracker", {}) == "SomeTracker"


# -- The devices map ----------------------------------------------------------


def model_subentry(model: str, devices: dict, **data) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType({CONF_MODEL: model, CONF_DEVICES: devices, **data}),
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=model,
        unique_id=model_key(model),
    )


def legacy_subentry(unique_id: str, **data) -> ConfigSubentry:
    """A sub-entry in the per-device shape this replaced."""
    return ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_UNIQUE_ID: unique_id, **data}),
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=unique_id,
        unique_id=unique_id,
    )


def test_devices_are_added_and_replaced() -> None:
    subentry = model_subentry("FTC921", {"a": {CONF_DEVICE_NAME: "A"}})
    updated = with_device(subentry, "b", {CONF_DEVICE_NAME: "B"})
    assert set(updated[CONF_DEVICES]) == {"a", "b"}
    assert updated[CONF_MODEL] == "FTC921"


def test_removing_a_device_leaves_the_rest() -> None:
    subentry = model_subentry("FTC921", {"a": {}, "b": {}})
    assert set(without_device(subentry, "a")[CONF_DEVICES]) == {"b"}


def test_a_missing_devices_map_reads_as_empty() -> None:
    """A legacy sub-entry has none, and must not raise on the way through."""
    assert device_entries(legacy_subentry("a")) == {}


def test_find_matches_on_the_model_key() -> None:
    subentry = model_subentry("FTC921", {})
    entry = SimpleNamespace(subentries={subentry.subentry_id: subentry})
    assert find_subentry(entry, "ftc921") is subentry
    assert find_subentry(entry, "FTC305") is None


# -- Migration and moves ------------------------------------------------------


class _Entries:
    """The slice of ConfigEntries that `async_sync_shape` touches."""

    def __init__(self, entry) -> None:
        self.entry = entry
        self.removed: list[str] = []

    def async_add_subentry(self, entry, subentry) -> None:
        entry.subentries[subentry.subentry_id] = subentry

    def async_update_subentry(self, entry, subentry, *, data) -> None:
        entry.subentries[subentry.subentry_id] = ConfigSubentry(
            data=MappingProxyType(data),
            subentry_id=subentry.subentry_id,
            subentry_type=subentry.subentry_type,
            title=subentry.title,
            unique_id=subentry.unique_id,
        )

    def async_remove_subentry(self, entry, subentry_id) -> None:
        self.removed.append(subentry_id)
        entry.subentries.pop(subentry_id, None)


@pytest.fixture
def world(monkeypatch):
    """A config entry plus stubbed device and entity registries."""
    entry = SimpleNamespace(entry_id="entry", subentries={})
    entries = _Entries(entry)
    hass = SimpleNamespace(config_entries=entries)

    moves: list[tuple[str, str, str]] = []
    device = SimpleNamespace(id="dev")

    devices = SimpleNamespace(
        async_get_device=lambda identifiers: device,
        async_update_device=lambda device_id, **kw: moves.append(
            ("device", device_id, str(sorted(kw.items())))
        ),
    )
    registered: list[SimpleNamespace] = []

    def move_entity(entity_id, **kw):
        moves.append(("entity", entity_id, kw.get("config_subentry_id")))
        # Actually applied, because the migration reads the registry back to
        # check nothing is still on the old sub-entry before removing it.
        for entry in registered:
            if entry.entity_id == entity_id:
                entry.config_subentry_id = kw["config_subentry_id"]

    entities = SimpleNamespace(async_update_entity=move_entity)

    monkeypatch.setattr(subentries_module.dr, "async_get", lambda _h: devices)
    monkeypatch.setattr(subentries_module.er, "async_get", lambda _h: entities)
    monkeypatch.setattr(
        subentries_module.er,
        "async_entries_for_config_entry",
        lambda _r, _e: list(registered),
    )
    return SimpleNamespace(
        hass=hass,
        entry=entry,
        entries=entries,
        moves=moves,
        registered=registered,
        device=device,
        entities_registry=entities,
    )


def add(world, subentry) -> ConfigSubentry:
    world.entry.subentries[subentry.subentry_id] = subentry
    return subentry


def test_legacy_subentries_fold_into_one_model_card(world) -> None:
    """The point of the whole change: three devices, one card."""
    for unique_id in ("a", "b", "c"):
        add(world, legacy_subentry(unique_id, **{CONF_DEVICE_NAME: unique_id}))

    async_sync_shape(
        world.hass, world.entry, dict.fromkeys(("a", "b", "c"), "FTC921"), {}
    )

    remaining = list(world.entry.subentries.values())
    assert len(remaining) == 1
    assert set(device_entries(remaining[0])) == {"a", "b", "c"}
    assert remaining[0].unique_id == model_key("FTC921")


def test_different_models_get_different_cards(world) -> None:
    add(world, legacy_subentry("a"))
    add(world, legacy_subentry("b"))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921", "b": "FTC305"}, {})

    assert {s.unique_id for s in world.entry.subentries.values()} == {
        model_key("FTC921"),
        model_key("FTC305"),
    }


def test_per_device_settings_survive_the_fold(world) -> None:
    """Overrides and the accuracy filter are the device's own and stay with it."""
    add(
        world,
        legacy_subentry(
            "a",
            **{
                CONF_DEVICE_NAME: "Cobalt",
                CONF_TRACCAR_DEVICE_ID: 18,
                CONF_ATTRIBUTE_OVERRIDES: [{"attribute": "io800"}],
                CONF_MAX_ACCURACY: 50,
            },
        ),
    )

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    settings = device_entries(next(iter(world.entry.subentries.values())))["a"]
    assert settings[CONF_DEVICE_NAME] == "Cobalt"
    assert settings[CONF_TRACCAR_DEVICE_ID] == 18
    assert settings[CONF_ATTRIBUTE_OVERRIDES] == [{"attribute": "io800"}]
    assert settings[CONF_MAX_ACCURACY] == 50


def test_a_profile_choice_moves_up_to_the_model(world) -> None:
    """It describes hardware, and the sub-entry is now the hardware.

    Dropping it silently undid a deliberate "use this profile" -- or "use no
    profile" -- on every upgrade.
    """
    add(world, legacy_subentry("a", **{CONF_DEVICE_PROFILE: "teltonika_ftc921"}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    card = next(iter(world.entry.subentries.values()))
    assert card.data[CONF_DEVICE_PROFILE] == "teltonika_ftc921"
    assert CONF_DEVICE_PROFILE not in device_entries(card)["a"]


def test_the_first_device_does_not_overrule_an_existing_card(world) -> None:
    """A fleet that already agreed on a profile keeps it."""
    add(world, model_subentry("FTC921", {"a": {}}, **{CONF_DEVICE_PROFILE: "chosen"}))
    add(world, legacy_subentry("b", **{CONF_DEVICE_PROFILE: "other"}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921", "b": "FTC921"}, {})

    card = next(iter(world.entry.subentries.values()))
    assert card.data[CONF_DEVICE_PROFILE] == "chosen"


def test_entities_are_moved_not_recreated(world) -> None:
    """Removing a sub-entry deletes its entities; history must not be lost."""
    legacy = add(world, legacy_subentry("a"))
    world.registered.append(
        SimpleNamespace(
            entity_id="sensor.a_speed",
            config_subentry_id=legacy.subentry_id,
            device_id="dev",
        )
    )

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    target = next(iter(world.entry.subentries.values())).subentry_id
    assert ("entity", "sensor.a_speed", target) in world.moves


def test_the_old_subentry_is_removed_after_the_move(world) -> None:
    legacy = add(world, legacy_subentry("a"))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    assert world.entries.removed == [legacy.subentry_id]


def test_a_device_whose_model_changed_moves_card(world) -> None:
    add(world, model_subentry("FTC305", {"a": {CONF_DEVICE_NAME: "A"}}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    remaining = list(world.entry.subentries.values())
    assert len(remaining) == 1
    assert remaining[0].unique_id == model_key("FTC921")
    assert set(device_entries(remaining[0])) == {"a"}


def test_a_device_traccar_stopped_reporting_is_left_where_it_is(world) -> None:
    """Absence is not a model change, and deciding it is gone is not ours."""
    add(world, model_subentry("FTC305", {"a": {}}))

    async_sync_shape(world.hass, world.entry, {}, {})

    assert set(device_entries(next(iter(world.entry.subentries.values())))) == {"a"}


def test_an_emptied_card_is_removed(world) -> None:
    """The last device of a model moving away leaves nothing behind."""
    add(world, model_subentry("FTC305", {"a": {}}))
    add(world, model_subentry("FTC921", {}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    assert {s.unique_id for s in world.entry.subentries.values()} == {
        model_key("FTC921")
    }


def test_a_settled_install_is_not_rewritten(world) -> None:
    """Nothing to do must mean no writes: this runs on every setup."""
    add(world, model_subentry("FTC921", {"a": {}}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    assert world.entries.removed == []
    assert world.moves == []


def test_devices_with_no_model_collect_in_one_card(world) -> None:
    add(world, legacy_subentry("a"))
    add(world, legacy_subentry("b"))

    async_sync_shape(world.hass, world.entry, {"a": None, "b": ""}, {})

    remaining = list(world.entry.subentries.values())
    assert len(remaining) == 1
    assert remaining[0].unique_id == "model:"
    assert set(device_entries(remaining[0])) == {"a", "b"}


def test_a_failed_fold_keeps_the_device(world, monkeypatch) -> None:
    """Removing the old card would delete the device and every entity on it.

    `known_devices` then stops auto-add putting it back, so the device would be
    gone for good. A stray card costs nothing by comparison, and the next setup
    tries again.
    """
    legacy = add(world, legacy_subentry("a"))
    monkeypatch.setattr(
        world.entries,
        "async_add_subentry",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")),
    )

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    assert legacy.subentry_id in world.entry.subentries
    assert world.entries.removed == []


def test_one_bad_fold_does_not_stop_the_others(world, monkeypatch) -> None:
    """Setup must not fail, and the rest of the fleet must still migrate."""
    add(world, legacy_subentry("a"))
    add(world, legacy_subentry("b"))

    real = world.entries.async_update_subentry
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("nope")
        return real(*args, **kwargs)

    monkeypatch.setattr(world.entries, "async_update_subentry", flaky)
    async_sync_shape(world.hass, world.entry, {"a": "FTC921", "b": "FTC921"}, {})

    folded = [s for s in world.entry.subentries.values() if device_entries(s)]
    assert folded, "the second device should still have migrated"


def test_two_devices_moving_from_one_card_both_arrive(world) -> None:
    """The source object goes stale after the first move; it has to be re-read."""
    add(world, model_subentry("FTC305", {"a": {}, "b": {}}))

    async_sync_shape(world.hass, world.entry, {"a": "FTC921", "b": "FTC921"}, {})

    remaining = list(world.entry.subentries.values())
    assert len(remaining) == 1
    assert set(device_entries(remaining[0])) == {"a", "b"}


def test_an_entity_with_no_device_is_still_moved(world) -> None:
    """It used to be skipped, and then deleted with the sub-entry.

    The registry filter matched on the device id as well, so an entity whose
    device could not be resolved stayed on the old sub-entry and went with it.
    """
    legacy = add(world, legacy_subentry("a"))
    world.registered.append(
        SimpleNamespace(
            entity_id="sensor.a_orphan",
            config_subentry_id=legacy.subentry_id,
            device_id="some-other-device",
        )
    )

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    target = next(iter(world.entry.subentries.values())).subentry_id
    assert ("entity", "sensor.a_orphan", target) in world.moves
    assert world.entries.removed == [legacy.subentry_id]


def test_a_subentry_with_entities_left_on_it_is_kept(world, monkeypatch) -> None:
    """Removing it would delete them, and `known_devices` stops auto-add
    bringing the device back."""
    legacy = add(world, legacy_subentry("a"))
    world.registered.append(
        SimpleNamespace(
            entity_id="sensor.a_speed",
            config_subentry_id=legacy.subentry_id,
            device_id="dev",
        )
    )
    # A move that silently does not take effect, which is what the check exists
    # to notice.
    monkeypatch.setattr(
        world.entities_registry, "async_update_entity", lambda *a, **k: None
    )

    async_sync_shape(world.hass, world.entry, {"a": "FTC921"}, {})

    assert legacy.subentry_id in world.entry.subentries
    assert world.entries.removed == []
