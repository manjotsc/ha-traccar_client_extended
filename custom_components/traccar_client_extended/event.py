"""Event platform for Traccar Client Extended.

Core's traccar_server fires raw ``traccar_<event>`` bus events, and its own
source comments acknowledge that this breaks two Home Assistant guidelines: it
should be an event entity, and bus events should be domain-prefixed. It keeps
them only for backwards compatibility.

Here they are proper event entities. The bus events are still fired, under this
integration's own domain prefix, so automations can use either.
"""

from __future__ import annotations

from typing import Any

from pytraccar import DeviceModel

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPES, SIGNAL_DISCOVERY, SIGNAL_EVENT
from .coordinator import TraccarClientExtendedCoordinator, TraccarConfigEntry
from .entity import TraccarClientExtendedEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TraccarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one event entity per device, when event import is enabled."""
    coordinator = entry.runtime_data
    created: set[str] = set()

    @callback
    def _discover() -> None:
        for record in (coordinator.data or {}).values():
            device = record["device"]
            unique_id = device["uniqueId"]
            if unique_id in created:
                continue
            subentry_id = coordinator.subentry_id_for(unique_id)
            if subentry_id is None:
                continue
            created.add(unique_id)
            async_add_entities(
                [TraccarEventEntity(coordinator, device)],
                config_subentry_id=subentry_id,
            )

    _discover()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discover))


class TraccarEventEntity(TraccarClientExtendedEntity, EventEntity):
    """Surface Traccar server events for a device."""

    _attr_name = "Event"
    # Advertise every type regardless of which are currently selected, so the
    # entity's capabilities stay stable when the options change. Includes the
    # per-alarm types, so an automation can trigger on `alarm_tow` rather than
    # inspecting the payload of a generic `alarm`.
    _attr_event_types: list[str] = list(EVENT_TYPES)  # noqa: RUF012 - read only

    def __init__(
        self,
        coordinator: TraccarClientExtendedCoordinator,
        device: DeviceModel,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device['uniqueId']}_event"

    async def async_added_to_hass(self) -> None:
        """Subscribe to events for this device."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_EVENT}_{self.device_id}",
                self._async_handle_event,
            )
        )

    @callback
    def _async_handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record an incoming Traccar event."""
        if event_type not in self.event_types:
            return
        self._trigger_event(event_type, payload)
        self.async_write_ha_state()
