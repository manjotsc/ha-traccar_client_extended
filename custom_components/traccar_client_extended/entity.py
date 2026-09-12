"""Base entity for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

from pytraccar import DeviceModel, GeofenceModel, PositionModel

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_DEVICE_UPDATE
from .coordinator import TraccarClientExtendedCoordinator, TraccarDeviceData


class TraccarClientExtendedEntity(CoordinatorEntity[TraccarClientExtendedCoordinator]):
    """Base entity carrying the device registry entry and data accessors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TraccarClientExtendedCoordinator,
        device: DeviceModel,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.device_id = device["id"]
        self.device_unique_id = device["uniqueId"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["uniqueId"])},
            name=device["name"],
            manufacturer="Traccar",
            # Traccar has no manufacturer field; `model` is free text set by the
            # user and `category` is the icon class, so fall back to it.
            model=device.get("model") or device.get("category") or "Unknown",
            serial_number=device["uniqueId"],
            configuration_url=coordinator.server_url,
        )

    @property
    def available(self) -> bool:
        """Return True when the coordinator still knows about this device."""
        return bool(
            super().available
            and self.coordinator.data
            and self.device_id in self.coordinator.data
        )

    @property
    def traccar_data(self) -> TraccarDeviceData:
        """Return the full record for this device."""
        return self.coordinator.data[self.device_id]

    @property
    def traccar_device(self) -> DeviceModel:
        """Return the device."""
        return self.traccar_data["device"]

    @property
    def traccar_position(self) -> PositionModel:
        """Return the latest accepted position."""
        return self.traccar_data["position"]

    @property
    def traccar_attributes(self) -> dict[str, Any]:
        """Return the position attributes."""
        return self.traccar_data["attributes"]

    @property
    def traccar_geofences(self) -> list[GeofenceModel]:
        """Return every geofence this device is currently inside."""
        return self.traccar_data["geofences"]

    async def async_added_to_hass(self) -> None:
        """Subscribe to the per-device signal used for websocket updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DEVICE_UPDATE}_{self.device_id}",
                self.async_write_ha_state,
            )
        )
