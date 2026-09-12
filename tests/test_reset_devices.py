"""Tests for the "Re-add deleted devices" options action.

Deleting a device records it as already seen, which is what makes the deletion
stick. This action clears that record so auto-add treats every device as new
again — the bulk undo, instead of re-adding devices one at a time.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.traccar_client_extended.config_flow import (
    OPTIONS_FLOW,
    _save_devices,
)
from custom_components.traccar_client_extended.const import (
    CONF_AUTO_ADD_DEVICES,
    CONF_CONFIRM,
    CONF_KNOWN_DEVICES,
)
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
)

from .conftest import step_keys


class _Entries:
    def __init__(self, entry) -> None:
        self.entry = entry
        self.reloaded: list[str] = []

    def async_update_entry(self, entry, *, data) -> None:
        entry.data = data

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


def handler(data: dict | None = None):
    """A stand-in for the slice of SchemaCommonFlowHandler the action uses."""
    entry = SimpleNamespace(
        entry_id="abc",
        data=dict(data if data is not None else {CONF_KNOWN_DEVICES: ["van", "car"]}),
        options={},
    )
    entries = _Entries(entry)
    return SimpleNamespace(
        parent_handler=SimpleNamespace(
            config_entry=entry, hass=SimpleNamespace(config_entries=entries)
        )
    )


# -- Flow wiring --------------------------------------------------------------


def test_options_flow_is_a_menu() -> None:
    """Each menu row is one thing somebody opens this dialog to change."""
    assert isinstance(OPTIONS_FLOW["init"], SchemaFlowMenuStep)
    assert "devices" in OPTIONS_FLOW["init"].options


def test_every_menu_entry_leads_somewhere() -> None:
    """A menu option with no step behind it is a dead row in the dialog."""
    for step_id in OPTIONS_FLOW["init"].options:
        assert step_id in OPTIONS_FLOW, step_id
        assert isinstance(OPTIONS_FLOW[step_id], SchemaFlowFormStep), step_id


def test_every_step_is_reachable_from_the_menu() -> None:
    """A step nobody can navigate to is settings the user cannot change."""
    reachable = set(OPTIONS_FLOW["init"].options) | {"init"}
    assert set(OPTIONS_FLOW) == reachable


def test_no_option_is_owned_by_two_steps() -> None:
    """Each step writes only its own keys, so an overlap would be ambiguous.

    SchemaCommonFlowHandler drops optional keys that are in a step's schema but
    absent from its submission. A key appearing in two steps would therefore be
    cleared by whichever step was saved without it.
    """
    seen: dict[str, str] = {}
    for step_id, step in OPTIONS_FLOW.items():
        if step_id == "init":
            continue
        for key in step_keys(step):
            assert str(key) not in seen, (key, step_id, seen.get(str(key)))
            seen[str(key)] = step_id


def test_devices_step_runs_the_re_add() -> None:
    """Re-adding lives with the device settings; both answer "which devices"."""
    assert OPTIONS_FLOW["devices"].validate_user_input is _save_devices
    assert "reset_devices" not in OPTIONS_FLOW


# -- The action ---------------------------------------------------------------


async def test_confirmed_reset_clears_the_known_list() -> None:
    h = handler()
    await _save_devices(h, {CONF_CONFIRM: True})
    assert CONF_KNOWN_DEVICES not in h.parent_handler.config_entry.data


async def test_confirmed_reset_reloads_the_entry() -> None:
    """Options are unchanged, so nothing else would trigger a reload."""
    h = handler()
    await _save_devices(h, {CONF_CONFIRM: True})
    assert h.parent_handler.hass.config_entries.reloaded == ["abc"]


@pytest.mark.parametrize("user_input", [{}, {CONF_CONFIRM: False}])
async def test_unconfirmed_reset_does_nothing(user_input: dict) -> None:
    """Saving the device settings must not re-add anything by itself.

    Sharing a step with a setting is what makes this matter: every save of
    "automatically add new devices" now passes through here, so an unticked box
    has to mean "leave my deletions alone" rather than raise.
    """
    h = handler()
    await _save_devices(h, user_input)

    assert h.parent_handler.config_entry.data[CONF_KNOWN_DEVICES] == ["van", "car"]
    assert h.parent_handler.hass.config_entries.reloaded == []


async def test_confirmation_is_not_stored_as_an_option() -> None:
    """A stored `confirm: true` would re-add every deletion on the next save.

    The tickbox shares a step with a real setting now, so it is submitted every
    time that setting changes. Storing it would turn one deliberate re-add into
    a permanent one.
    """
    h = handler()

    assert CONF_CONFIRM not in await _save_devices(h, {CONF_CONFIRM: True})


async def test_the_device_setting_is_saved_alongside() -> None:
    """Merging the steps must not drop the setting the step is named for."""
    h = handler()

    assert await _save_devices(h, {CONF_AUTO_ADD_DEVICES: False}) == {
        CONF_AUTO_ADD_DEVICES: False
    }


async def test_other_entry_data_is_preserved() -> None:
    """Only the known-devices record is dropped; credentials must survive."""
    h = handler(
        {"host": "traccar.example", "api_token": "secret", CONF_KNOWN_DEVICES: ["van"]}
    )
    await _save_devices(h, {CONF_CONFIRM: True})

    assert h.parent_handler.config_entry.data == {
        "host": "traccar.example",
        "api_token": "secret",
    }


async def test_reset_with_nothing_recorded_is_harmless() -> None:
    """Running it twice, or on a fresh install, must not raise."""
    h = handler({})
    await _save_devices(h, {CONF_CONFIRM: True})
    assert h.parent_handler.config_entry.data == {}
    assert h.parent_handler.hass.config_entries.reloaded == ["abc"]
