"""Device tracker platform for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

from pytraccar import DeviceModel

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_CATEGORY,
    ATTR_GEOFENCES,
    ATTR_TRACCAR_ID,
    ATTR_TRACKER,
    DOMAIN,
    SIGNAL_DISCOVERY,
)
from .coordinator import TraccarClientExtendedCoordinator, TraccarConfigEntry
from .entity import TraccarClientExtendedEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TraccarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a device tracker per device, including devices added later."""
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
                [TraccarDeviceTracker(coordinator, device)],
                config_subentry_id=subentry_id,
            )

    _discover()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discover))


class TraccarDeviceTracker(TraccarClientExtendedEntity, TrackerEntity):
    """Represent a tracked device."""

    _attr_name = None

    def __init__(
        self,
        coordinator: TraccarClientExtendedCoordinator,
        device: DeviceModel,
    ) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator, device)
        self._attr_unique_id = device["uniqueId"]

    @property
    def latitude(self) -> float | None:
        """Return the latitude of the last accepted position."""
        if not self.available:
            return None
        return self.traccar_position.get("latitude")

    @property
    def longitude(self) -> float | None:
        """Return the longitude of the last accepted position."""
        if not self.available:
            return None
        return self.traccar_position.get("longitude")

    @property
    def location_accuracy(self) -> float:
        """Return the GPS accuracy in metres."""
        if not self.available:
            return 0
        return self.traccar_position.get("accuracy") or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device specific attributes."""
        if not self.available:
            return {}

        device = self.traccar_device
        attributes: dict[str, Any] = {
            ATTR_CATEGORY: device.get("category"),
            ATTR_TRACCAR_ID: device["id"],
            ATTR_TRACKER: DOMAIN,
            # Every matching geofence, not just the first one.
            ATTR_GEOFENCES: [geofence["name"] for geofence in self.traccar_geofences],
        }

        # Opt-in passthrough, kept for parity with core's traccar_server so
        # existing templates keep working after a migration.
        position_attributes = self.traccar_attributes
        device_attributes = device.get("attributes") or {}
        for name in self.coordinator.custom_attributes:
            attributes[name] = device_attributes.get(
                name, position_attributes.get(name)
            )

        return attributes
