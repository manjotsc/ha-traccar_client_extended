"""Sensor platform for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

from pytraccar import DeviceModel

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import DEGREE, EntityCategory, UnitOfLength, UnitOfSpeed
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .attributes import (
    TraccarSensorEntityDescription,
    describe,
)
from .const import SIGNAL_DISCOVERY
from .coordinator import (
    TraccarClientExtendedCoordinator,
    TraccarConfigEntry,
    TraccarDeviceData,
)
from .entity import TraccarClientExtendedEntity
from .helpers import parse_timestamp

# Entities built from top-level position and device fields. Core's
# traccar_server fetches all of these and exposes none of them.
FIXED_SENSORS: tuple[TraccarSensorEntityDescription, ...] = (
    TraccarSensorEntityDescription(
        key="bearing",
        name="Bearing",
        icon="mdi:compass-outline",
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data["position"].get("course"),
    ),
    TraccarSensorEntityDescription(
        key="speed",
        name="Speed",
        device_class=SensorDeviceClass.SPEED,
        # Traccar stores speed in knots regardless of the server's display
        # setting, so declare knots and let Home Assistant convert.
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data["position"].get("speed"),
    ),
    TraccarSensorEntityDescription(
        key="altitude",
        name="Altitude",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data["position"].get("altitude"),
    ),
    TraccarSensorEntityDescription(
        key="accuracy",
        name="GPS accuracy",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data["position"].get("accuracy"),
    ),
    TraccarSensorEntityDescription(
        key="address",
        # Reports the staleness rather than being invalidated by it.
        name="Address",
        icon="mdi:map-marker",
        value_fn=lambda data: data["position"].get("address"),
    ),
    TraccarSensorEntityDescription(
        key="geofence",
        # Reports the staleness rather than being invalidated by it.
        name="Geofence",
        icon="mdi:map-marker-radius",
        value_fn=lambda data: (
            geofences[0]["name"] if (geofences := data["geofences"]) else None
        ),
        # Core keeps only the first match; expose the full set alongside it so
        # overlapping geofences are not lost.
        extra_attributes_fn=lambda data: {
            "geofences": [geofence["name"] for geofence in data["geofences"]]
        },
    ),
    # The "last seen" family. Core exposes none of these, which is the
    # long-standing complaint in home-assistant/core#76167.
    TraccarSensorEntityDescription(
        key="fix_time",
        # Reports the staleness rather than being invalidated by it.
        name="Last fix",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: parse_timestamp(data["position"].get("fixTime")),
    ),
    TraccarSensorEntityDescription(
        key="last_update",
        # Reports the staleness rather than being invalidated by it.
        name="Last update",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: parse_timestamp(data["device"].get("lastUpdate")),
    ),
    TraccarSensorEntityDescription(
        key="device_time",
        # Reports the staleness rather than being invalidated by it.
        name="Device time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: parse_timestamp(data["position"].get("deviceTime")),
    ),
    TraccarSensorEntityDescription(
        key="server_time",
        # Reports the staleness rather than being invalidated by it.
        name="Server time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: parse_timestamp(data["position"].get("serverTime")),
    ),
    TraccarSensorEntityDescription(
        key="protocol",
        # Reports the staleness rather than being invalidated by it.
        name="Protocol",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data["position"].get("protocol"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TraccarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, adding more as devices and attributes appear."""
    coordinator = entry.runtime_data
    created: set[str] = set()

    @callback
    def _discover() -> None:
        by_subentry: dict[str, list[TraccarSensor]] = {}
        for device_id, record in (coordinator.data or {}).items():
            device = record["device"]
            unique_prefix = device["uniqueId"]
            # A device only produces entities once it has a subentry, which is
            # what makes the device list opt-in.
            subentry_id = coordinator.subentry_id_for(unique_prefix)
            if subentry_id is None:
                continue
            overrides = coordinator.overrides_for(unique_prefix)

            descriptions: list[TraccarSensorEntityDescription] = list(FIXED_SENSORS)
            for key, value in coordinator.discoverable_attributes(device_id).items():
                description = describe(key, value, overrides.get(key))
                if isinstance(description, TraccarSensorEntityDescription):
                    descriptions.append(description)

            for description in descriptions:
                unique_id = f"{unique_prefix}_{description.key}"
                if unique_id in created:
                    continue
                created.add(unique_id)
                by_subentry.setdefault(subentry_id, []).append(
                    TraccarSensor(coordinator, device, description)
                )

        for subentry_id, entities in by_subentry.items():
            async_add_entities(entities, config_subentry_id=subentry_id)

    _discover()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discover))


class TraccarSensor(TraccarClientExtendedEntity, SensorEntity):
    """A Traccar sensor, from either a position field or an attribute."""

    entity_description: TraccarSensorEntityDescription

    def __init__(
        self,
        coordinator: TraccarClientExtendedCoordinator,
        device: DeviceModel,
        description: TraccarSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = f"{device['uniqueId']}_{description.key}"

    def _resolve(self, data: TraccarDeviceData) -> Any:
        """Return the raw value for this description.

        Fixed entities read a position or device field through ``value_fn``;
        attribute entities read ``position["attributes"]`` by key.
        """
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(data)
        if (key := self.entity_description.attribute_key) is not None:
            return data["attributes"].get(key)
        return None

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if not self.available:
            return None
        value = self._resolve(self.traccar_data)
        # A numeric sensor handed a bool would render as True/False; Traccar
        # occasionally flips an attribute's type between reports.
        if isinstance(value, bool) and self.entity_description.state_class is not None:
            value = int(value)
        # Raw protocol values often need scaling, e.g. Teltonika reports
        # voltages in millivolts under an unnamed io<id> key.
        if (scale := self.entity_description.scale) != 1.0 and isinstance(
            value, (int, float)
        ):
            return value * scale
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return supplementary attributes, where the description defines them."""
        if not self.available or self.entity_description.extra_attributes_fn is None:
            return None
        return self.entity_description.extra_attributes_fn(self.traccar_data)
