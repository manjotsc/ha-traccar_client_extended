"""Tests for discovery notification and the device lookup index.

Both are on the per-update path, so both are written to do as little work as
possible. These tests pin the behaviour that makes that safe.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.traccar_client_extended.coordinator as coordinator_module
from custom_components.traccar_client_extended.coordinator import (
    TraccarClientExtendedCoordinator,
)

# _notify_discovery is a @callback; unwrap it so it can be called directly.
NOTIFY = getattr(
    TraccarClientExtendedCoordinator._notify_discovery,
    "__wrapped__",
    TraccarClientExtendedCoordinator._notify_discovery,
)


@pytest.fixture
def dispatched(monkeypatch):
    """Record every discovery signal instead of sending it."""
    calls: list[int] = []
    monkeypatch.setattr(
        coordinator_module, "async_dispatcher_send", lambda *a, **k: calls.append(1)
    )
    return calls


def build(devices: dict[int, dict], **overrides):
    c = object.__new__(TraccarClientExtendedCoordinator)
    c.hass = SimpleNamespace(data={})
    c.config_entry = SimpleNamespace(subentries={}, entry_id="entry")
    # Read by the prune pass that runs whenever discovery is dispatched.
    c.attribute_overrides = {}
    c.ignored_attributes = frozenset()
    c.profiles = {}
    c.data = {
        device_id: {
            "device": {"uniqueId": f"d{device_id}", "status": "online"},
            "position": {},
            "geofences": [],
            "attributes": dict(attributes),
        }
        for device_id, attributes in devices.items()
    }
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


# -- Discovery diffing --------------------------------------------------------


def test_first_call_notifies(dispatched) -> None:
    """Platforms have to be told about the devices that already exist."""
    NOTIFY(build({1: {"io1": 1}}))
    assert len(dispatched) == 1


def test_unchanged_update_does_not_notify(dispatched) -> None:
    """The common case: a position arrives and nothing about the shape changed."""
    c = build({1: {"io1": 1}})
    NOTIFY(c)
    dispatched.clear()
    for _ in range(10):
        NOTIFY(c, {1})
    assert dispatched == []


def test_new_attribute_notifies(dispatched) -> None:
    """A key appearing later must create its entity without a restart."""
    c = build({1: {"io1": 1}})
    NOTIFY(c)
    dispatched.clear()
    c.data[1]["attributes"]["io2"] = 2
    NOTIFY(c, {1})
    assert len(dispatched) == 1


def test_removed_attribute_notifies(dispatched) -> None:
    c = build({1: {"io1": 1, "io2": 2}})
    NOTIFY(c)
    dispatched.clear()
    del c.data[1]["attributes"]["io2"]
    NOTIFY(c, {1})
    assert len(dispatched) == 1


def test_new_device_notifies_even_without_being_named(dispatched) -> None:
    """Device set changes are always checked, whatever `changed` says."""
    c = build({1: {"io1": 1}})
    NOTIFY(c)
    dispatched.clear()
    c.data[2] = dict(c.data[1])
    NOTIFY(c, {1})
    assert len(dispatched) == 1


def test_removed_device_notifies(dispatched) -> None:
    c = build({1: {"io1": 1}, 2: {"io1": 1}})
    NOTIFY(c)
    dispatched.clear()
    del c.data[2]
    NOTIFY(c)
    assert len(dispatched) == 1


def test_full_refresh_checks_every_device(dispatched) -> None:
    """Passing no `changed` set means a full fetch, so check everything."""
    c = build({1: {"io1": 1}, 2: {"io1": 1}})
    NOTIFY(c)
    dispatched.clear()
    c.data[2]["attributes"]["ioNEW"] = 1
    NOTIFY(c)
    assert len(dispatched) == 1


def test_changed_set_is_trusted() -> None:
    """The optimisation rests on an invariant worth stating.

    handle_subscription_data adds every device it mutates to `updated`, so a
    device that changed can never be missing from `changed`. If that ever stops
    being true, this narrower scan would miss it.
    """
    import inspect

    source = inspect.getsource(
        TraccarClientExtendedCoordinator.handle_subscription_data
    )
    # Every assignment into self.data is paired with an updated.add(...).
    assert source.count("updated.add(device_id)") == 2
    assert source.count("self.data[device_id]") == 2


# -- Lookup index -------------------------------------------------------------


def test_index_answers_by_unique_id() -> None:
    c = build({1: {}, 2: {}})
    assert c.record_for("d2")["device"]["uniqueId"] == "d2"
    assert c.record_for("nobody") is None
    assert c.record_for(None) is None


def test_index_follows_in_place_updates(dispatched) -> None:
    """The websocket path mutates the same dict, so identity cannot detect it.

    Without invalidation the index would keep serving the record a device had
    when it was first built.
    """
    c = build({1: {}})
    assert c.record_for("d1")["device"]["status"] == "online"

    c.data[1]["device"]["status"] = "offline"
    NOTIFY(c, {1})
    assert c.record_for("d1")["device"]["status"] == "offline"


def test_index_picks_up_a_new_device(dispatched) -> None:
    c = build({1: {}})
    NOTIFY(c)
    assert c.record_for("d2") is None

    c.data[2] = {
        "device": {"uniqueId": "d2", "status": "online"},
        "position": {},
        "geofences": [],
        "attributes": {},
    }
    NOTIFY(c)
    assert c.record_for("d2") is not None
