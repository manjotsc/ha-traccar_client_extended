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
    decode_text,
    describe,
    describe_fields,
    extract_bits,
    value_label,
)
from .const import ATTR_RAW_VALUE, ATTR_TRACCAR_OPERATOR, SIGNAL_DISCOVERY
from .coordinator import (
    TraccarClientExtendedCoordinator,
    TraccarConfigEntry,
    TraccarDeviceData,
)
from .entity import TraccarClientExtendedEntity
from .helpers import parse_timestamp
from .operators import operator_name

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
                override = overrides.get(key)
                description = describe(key, value, override)
                if isinstance(description, TraccarSensorEntityDescription):
                    descriptions.append(description)
                # A packed parameter yields a sensor per field as well as, or
                # instead of, one for the word itself.
                descriptions.extend(
                    field
                    for field in describe_fields(key, override)
                    if isinstance(field, TraccarSensorEntityDescription)
                )

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
        # A PLMN identifier is an encoding of a name, not a measurement, so it
        # is decoded before anything numeric is considered. An unlisted code
        # falls through unchanged rather than becoming Unknown.
        if self.entity_description.decode_operator:
            return operator_name(self.coordinator.operators, value) or value
        # Traccar hex-dumps every variable-length parameter it has no special
        # case for, so an ASCII one arrives as twice as many hex characters.
        if (kind := self.entity_description.decode) is not None:
            return decode_text(kind, value)
        # One reading packed inside another, e.g. Teltonika's io1148 carrying
        # RSSI, RSRP, SINR and RSRQ in the four bytes of one 32-bit word.
        # Done before the labels, so a mode packed into a few bits can carry
        # them too.
        value = extract_bits(
            value,
            self.entity_description.bit_offset,
            self.entity_description.bit_width,
            signed=self.entity_description.signed,
        )
        # An enumerated reading -- a mode or a status byte -- reads as the label
        # its profile gives it, and as the number when the profile has none.
        if (labels := self.entity_description.value_labels) is not None:
            return value_label(labels, value) or value
        # A numeric sensor handed a bool would render as True/False; Traccar
        # occasionally flips an attribute's type between reports.
        if isinstance(value, bool) and self.entity_description.state_class is not None:
            value = int(value)
        # Raw protocol values often need scaling, e.g. Teltonika reports
        # voltages in millivolts under an unnamed io<id> key, and a packed byte
        # holding an unsigned magnitude needs a sign putting back on it.
        scale = self.entity_description.scale
        offset = self.entity_description.offset
        if (scale != 1.0 or offset != 0.0) and isinstance(value, (int, float)):
            return value * scale + offset
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return supplementary attributes, where the description defines them."""
        if not self.available:
            return None
        # Published whether or not the decode found a name, so a template
        # reading the code does not have to care which happened.
        if self.entity_description.decode_operator:
            return {ATTR_TRACCAR_OPERATOR: self._resolve(self.traccar_data)}
        if (
            self.entity_description.value_labels is not None
            or self.entity_description.decode is not None
        ):
            return {ATTR_RAW_VALUE: self._resolve(self.traccar_data)}
        if self.entity_description.extra_attributes_fn is None:
            return None
        return self.entity_description.extra_attributes_fn(self.traccar_data)
