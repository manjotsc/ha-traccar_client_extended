"""Tests for automatic device adding.

The distinction being protected: "new" means never seen before, not merely
lacking a subentry. A device the user deleted also lacks one, and recreating it
made deletion impossible — the delete appeared to work, then the device came
back on the next refresh and the add dialog reported everything was already
added.
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest

from custom_components.traccar_client_extended.const import (
    CONF_DEVICE_UNIQUE_ID,
    CONF_KNOWN_DEVICES,
    SUBENTRY_TYPE_DEVICE,
)
from homeassistant.config_entries import ConfigSubentry

from .test_coordinator_logic import make_coordinator


class _Entries:
    """The slice of ConfigEntries that sync_subentries touches."""

    def __init__(self, entry) -> None:
        self.entry = entry
        self.added: list[ConfigSubentry] = []

    def async_add_subentry(self, entry, subentry) -> None:
        self.added.append(subentry)
        entry.subentries[subentry.subentry_id] = subentry

    def async_update_entry(self, entry, *, data) -> None:
        entry.data = data


def coordinator(devices, *, known=(), auto_add=True, subentries=()):
    c = make_coordinator(auto_add_devices=auto_add)
    entry = SimpleNamespace(
        data={CONF_KNOWN_DEVICES: list(known)} if known else {},
        subentries={s.subentry_id: s for s in subentries},
    )
    c.config_entry = entry
    c.hass = SimpleNamespace(config_entries=_Entries(entry))
    c._devices = devices
    return c


def device(unique_id: str, device_id: int = 1) -> dict:
    return {"id": device_id, "uniqueId": unique_id, "name": unique_id.title()}


def subentry(unique_id: str) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_UNIQUE_ID: unique_id}),
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=unique_id,
        unique_id=unique_id,
    )


def added(c) -> list[str]:
    return [s.data[CONF_DEVICE_UNIQUE_ID] for s in c.hass.config_entries.added]


def known_after(c) -> list[str]:
    return c.config_entry.data.get(CONF_KNOWN_DEVICES, [])


# -- The reported bug ---------------------------------------------------------


def test_deleted_device_is_not_recreated() -> None:
    """The bug: deleting a device, then having auto-add put it straight back."""
    devices = [device("van", 1), device("car", 2)]
    c = coordinator(devices, known=["van", "car"], subentries=[subentry("car")])

    c.sync_subentries(devices)

    assert added(c) == [], "a device already seen must never be re-added"


def test_deleted_device_becomes_available_to_add_again() -> None:
    """And having stayed deleted, it is offered by the add dialog."""
    devices = [device("van", 1)]
    c = coordinator(devices, known=["van"])
    c.sync_subentries(devices)
    assert c.subentry_id_for("van") is None


# -- Normal behaviour ---------------------------------------------------------


def test_genuinely_new_device_is_added() -> None:
    devices = [device("van", 1), device("new", 2)]
    c = coordinator(devices, known=["van"], subentries=[subentry("van")])
    c.sync_subentries(devices)
    assert added(c) == ["new"]


def test_first_run_adds_everything() -> None:
    """Nothing is known yet, so every device is new."""
    devices = [device("van", 1), device("car", 2)]
    c = coordinator(devices)
    c.sync_subentries(devices)
    assert sorted(added(c)) == ["car", "van"]


def test_migration_records_existing_devices_without_re_adding() -> None:
    """An install predating this change already has subentries and no record."""
    devices = [device("van", 1)]
    c = coordinator(devices, subentries=[subentry("van")])
    c.sync_subentries(devices)
    assert added(c) == []
    assert known_after(c) == ["van"]


# -- Bookkeeping --------------------------------------------------------------


def test_devices_are_recorded_as_known() -> None:
    devices = [device("van", 1), device("car", 2)]
    c = coordinator(devices)
    c.sync_subentries(devices)
    assert known_after(c) == ["car", "van"]


def test_known_list_is_not_rewritten_when_unchanged() -> None:
    """Avoids a config entry write on every single refresh."""
    devices = [device("van", 1)]
    c = coordinator(devices, known=["van"], subentries=[subentry("van")])
    c.config_entry.data = MappingProxyType({CONF_KNOWN_DEVICES: ["van"]})
    c.sync_subentries(devices)  # would raise if it tried to mutate the proxy
    assert known_after(c) == ["van"]


def test_devices_recorded_even_when_auto_add_is_off() -> None:
    """Otherwise switching auto-add on later would sweep up every device."""
    devices = [device("van", 1)]
    c = coordinator(devices, auto_add=False)
    c.sync_subentries(devices)
    assert added(c) == []
    assert known_after(c) == ["van"]


def test_auto_add_off_adds_nothing_new() -> None:
    devices = [device("van", 1)]
    c = coordinator(devices, auto_add=False)
    c.sync_subentries(devices)
    assert added(c) == []


@pytest.mark.parametrize("bad", [None, ""])
def test_devices_without_a_unique_id_are_skipped(bad) -> None:
    devices = [{"id": 1, "uniqueId": bad, "name": "x"}, device("van", 2)]
    c = coordinator(devices)
    c.sync_subentries(devices)
    assert added(c) == ["van"]
    assert known_after(c) == ["van"]
