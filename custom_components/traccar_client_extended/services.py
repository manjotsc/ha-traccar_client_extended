"""Actions exposed by Traccar Client Extended.

`fire_test_event` publishes a synthetic Traccar event through the same code path
a real one takes, so an automation can be tested without waiting for the vehicle
to actually be towed.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    ALARMS,
    ATTR_EVENT_TYPE,
    DOMAIN,
    EVENT_TYPES,
    EVENTS,
    SERVICE_FIRE_TEST_EVENT,
)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_EVENT_TYPE): vol.In(EVENT_TYPES),
    }
)


def traccar_event_for(event_type: str) -> tuple[str, str | None] | None:
    """Map a published event type back to the Traccar event it came from.

    Alarms are the interesting case: every one arrives as a single `alarm`
    event, so `alarm_tow` has to become type `alarm` with `alarm: tow` in the
    attributes, exactly as Traccar would send it.
    """
    if event_type.startswith("alarm_"):
        wanted = event_type.removeprefix("alarm_")
        for value, name in ALARMS.items():
            if name == wanted:
                return "alarm", value
        return None

    for traccar_type, published in EVENTS.items():
        if published == event_type:
            return traccar_type, None
    return None


def _coordinators_for_device(hass: HomeAssistant, device_id: str):
    """Yield (coordinator, Traccar device id) for a Home Assistant device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="device_not_found"
        )

    unique_ids = {
        identifier for domain, identifier in device.identifiers if domain == DOMAIN
    }
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = entry.runtime_data
        for unique_id in unique_ids:
            if (record := coordinator.record_for(unique_id)) is not None:
                yield coordinator, record["device"]["id"]


async def _async_fire_test_event(call: ServiceCall) -> None:
    """Publish a synthetic event for each targeted device."""
    event_type = call.data[ATTR_EVENT_TYPE]
    if (resolved := traccar_event_for(event_type)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_event_type",
            translation_placeholders={"event_type": event_type},
        )
    traccar_type, alarm = resolved

    fired = False
    for device_id in call.data["device_id"]:
        for coordinator, traccar_device_id in _coordinators_for_device(
            call.hass, device_id
        ):
            attributes: dict[str, object] = {"test": True}
            if alarm is not None:
                attributes["alarm"] = alarm
            coordinator.publish_event(
                {
                    # No id, so repeated tests are never suppressed by the
                    # duplicate filter that guards real events.
                    "type": traccar_type,
                    "deviceId": traccar_device_id,
                    "eventTime": None,
                    "attributes": attributes,
                },
                source="action",
            )
            fired = True

    if not fired:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="device_not_tracked"
        )


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's actions, once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_FIRE_TEST_EVENT):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_FIRE_TEST_EVENT, _async_fire_test_event, schema=SERVICE_SCHEMA
    )
