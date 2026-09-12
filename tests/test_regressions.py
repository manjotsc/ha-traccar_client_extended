"""Guards against defects found while running on a real Home Assistant.

Each of these shipped once. They are cheap to assert and expensive to rediscover,
because none of them fail at import time — they only surface in a live install.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from custom_components.traccar_client_extended import config_flow, const, device_tracker
from homeassistant.config_entries import OptionsFlowWithReload

from .conftest import step_keys

PACKAGE = Path(config_flow.__file__).parent


def source(module: str) -> str:
    return (PACKAGE / module).read_text(encoding="utf-8")


def test_no_config_entry_update_listener() -> None:
    """Update listeners cannot coexist with automatic reload.

    Registering one made every sub-entry save raise
    "Cannot update and reload entry with update listeners", so applying a device
    profile failed with an unexplained error in the UI.
    """
    assert "add_update_listener" not in source("__init__.py")


def test_options_flow_reloads_itself() -> None:
    """With no update listener, the options flow has to trigger its own reload.

    Otherwise changing an option would be saved and silently never applied.
    """
    text = source("config_flow.py")
    assert "SchemaOptionsFlowHandlerWithReload(" in text
    assert "SchemaOptionsFlowHandler(" not in text

    from homeassistant.helpers.schema_config_entry_flow import (
        SchemaOptionsFlowHandlerWithReload,
    )

    assert issubclass(SchemaOptionsFlowHandlerWithReload, OptionsFlowWithReload)


def test_subentry_edits_reload_the_entry() -> None:
    """Entity descriptions are built at platform setup.

    Updating a sub-entry without reloading stored the new profile and never
    applied it, so the device kept its old, raw attribute mapping.
    """
    text = source("config_flow.py")
    assert "async_update_reload_and_abort(" in text
    # The non-reloading variant would silently reintroduce the bug.
    assert "async_update_and_abort(" not in text


def test_subentry_forms_hand_rejected_input_back() -> None:
    """Validating the overrides table is what made losing it possible.

    The options flow gets this for free: Home Assistant's schema helper merges
    `user_input` back into the suggested values when validation raises. The
    subentry flow is hand-rolled and has to do it itself, or one mistyped unit
    empties a table that took real work to fill in.
    """
    handler = config_flow.TraccarDeviceSubentryFlowHandler
    for step in (handler.async_step_user, handler.async_step_reconfigure):
        assert "add_suggested_values_to_schema" in inspect.getsource(step)


def test_subentry_lookup_is_not_cached() -> None:
    """Editing a sub-entry replaces the object in config_entry.subentries.

    A lookup built once at startup kept serving the device's pre-configuration
    settings, so a profile appeared to do nothing even after a reload.
    """
    text = source("coordinator.py")
    assert "self._subentries" not in text
    assert "_subentry_map" in text


def test_tracker_does_not_override_deprecated_battery_level() -> None:
    """BaseTrackerEntity.battery_level is deprecated and warns on every start.

    The batteryLevel attribute already produces a proper battery sensor.
    """
    assert "battery_level" not in device_tracker.TraccarDeviceTracker.__dict__


def test_subscribe_loop_honours_cancellation() -> None:
    """pytraccar swallows CancelledError inside its own subscribe().

    Without an explicit check the reconnect loop survived a config entry unload,
    and Home Assistant logged that the task "did not complete in time".
    """
    text = inspect.getsource(
        __import__(
            "custom_components.traccar_client_extended.coordinator",
            fromlist=["TraccarClientExtendedCoordinator"],
        ).TraccarClientExtendedCoordinator.subscribe
    )
    assert "cancelling()" in text
    assert "except asyncio.CancelledError" in text


def test_reconnect_resyncs_device_state() -> None:
    """Traccar replays positions on websocket open, but never device records.

    Without an explicit refresh, a status change that happened during the
    outage never reaches us: the device stays marked offline and every live
    sensor stays unavailable, even though positions are arriving again.
    """
    text = inspect.getsource(
        __import__(
            "custom_components.traccar_client_extended.coordinator",
            fromlist=["TraccarClientExtendedCoordinator"],
        ).TraccarClientExtendedCoordinator.subscribe
    )
    assert "async_request_refresh" in text


def test_expired_token_starts_reauth_rather_than_dying() -> None:
    """ConfigEntryAuthFailed only becomes a reauth flow from setup or a refresh.

    Raised from the subscription background task it just kills the task, taking
    the reconnect loop with it: no prompt, no retry, and live updates stop
    silently until Home Assistant is restarted.
    """
    text = inspect.getsource(
        __import__(
            "custom_components.traccar_client_extended.coordinator",
            fromlist=["TraccarClientExtendedCoordinator"],
        ).TraccarClientExtendedCoordinator.subscribe
    )
    assert "async_start_reauth" in text
    assert "raise ConfigEntryAuthFailed" not in text


def test_diagnostics_classify_through_profiles_and_overrides() -> None:
    """Reporting the built-in mapping would misdescribe every profiled device.

    Diagnostics exists to answer "why is this attribute wrong", so classifying
    it differently from the platforms defeats the point.
    """
    text = source("diagnostics.py")
    assert "overrides_for" in text
    assert "describe(key, value, overrides.get(key))" in text


# -- Translations -------------------------------------------------------------
#
# A missing string does not raise: Home Assistant renders the raw key, so a
# repair shows as "server_buffering" and a menu option as a blank line. Both
# only surface once the flow is in front of a user.


def _strings() -> dict:
    return json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))


def test_every_override_error_has_a_string() -> None:
    """An error key with no translation renders as the raw key in the form.

    `attribute_ignored_and_overridden` shipped that way: the check fired, and
    the user was shown the identifier instead of what to do about it.
    """
    from custom_components.traccar_client_extended import attributes

    keys = {
        # Returned through named constants...
        attributes.DEVICE_CLASS_UNIT_INVALID,
        attributes.DEVICE_CLASS_UNIT_MISSING,
        # ...and one returned as a literal, which is how it escaped notice.
        *re.findall(
            r'return "([a-z_]+)"', inspect.getsource(config_flow.check_override_rows)
        ),
    }
    assert len(keys) > 2, "the literal scrape found nothing; was the check renamed?"

    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
    options = set(strings["options"]["error"])
    assert keys <= options

    # The subentry form runs the same device-class check and shows its own errors.
    subentry = set(strings["config_subentries"]["device"]["error"])
    assert attributes.DEVICE_CLASS_UNIT_INVALID in subentry
    assert attributes.DEVICE_CLASS_UNIT_MISSING in subentry


def test_every_repair_issue_has_strings() -> None:
    """Every ISSUE_ constant is a translation key the moment it is raised."""
    issues = _strings()["issues"]
    missing = [
        value
        for name, value in vars(const).items()
        if name.startswith("ISSUE_") and value not in issues
    ]
    assert not missing


def test_every_menu_option_has_a_step_and_a_label() -> None:
    """A menu option without a step is a dead end, without a label a blank row."""
    config = _strings()["config"]
    options = re.findall(r"menu_options=\[([^\]]+)\]", source("config_flow.py"))
    assert options, "the user step no longer offers a menu"
    names = re.findall(r'"(\w+)"', options[0])

    for name in names:
        assert name in config["step"], name
        assert name in config["step"]["user"]["menu_options"], name


def test_shipped_translation_matches_the_source_strings() -> None:
    """en.json is what actually renders; strings.json is only the source."""
    shipped = json.loads(
        (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
    )

    def shape(value):
        if isinstance(value, dict):
            return {key: shape(item) for key, item in sorted(value.items())}
        return None

    assert shape(shipped) == shape(_strings())


def test_every_traccar_event_has_a_readable_label() -> None:
    """The pickers must show Traccar's names, not its API strings.

    Both the options picker and the repair picker offer all 23 types, labelled
    from EVENT_LABELS. A type added to EVENTS without a label raises KeyError
    when the form is built; one left behind after a rename would render as raw
    camelCase -- `proximityExit` rather than "Linked device away" -- matching
    nothing the user has seen in Traccar's own UI.

    The labels live in code rather than in strings.json because Traccar's type
    names are camelCase and a translation key must match [a-z0-9-_]+.
    """
    labels = const.EVENT_LABELS

    assert set(labels) == set(const.EVENTS), {
        "unlabelled": sorted(set(const.EVENTS) - set(labels)),
        "orphan_labels": sorted(set(labels) - set(const.EVENTS)),
    }


def test_every_option_has_a_label_in_its_own_step() -> None:
    """Splitting one form into four is where strings get left behind.

    A key whose label lives under the wrong step renders as the raw option name
    -- `max_discovered_attributes` rather than a sentence -- and nothing fails
    until the dialog is open in front of someone.
    """
    steps = _strings()["options"]["step"]

    for step_id, step in config_flow.OPTIONS_FLOW.items():
        if step_id == "init":
            continue
        labels = steps.get(step_id, {}).get("data", {})
        keys = {str(key) for key in step_keys(step)}
        assert keys <= set(labels), {
            "step": step_id,
            "unlabelled": sorted(keys - set(labels)),
        }


def test_every_options_menu_entry_has_a_label() -> None:
    """Otherwise the menu shows a blank row that still navigates somewhere."""
    init = _strings()["options"]["step"]["init"]["menu_options"]
    assert set(config_flow.OPTIONS_FLOW["init"].options) == set(init)
