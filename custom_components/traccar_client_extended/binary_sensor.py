"""Binary sensor platform for Traccar Client Extended."""

from __future__ import annotations

from pytraccar import DeviceModel

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .attributes import (
    TraccarBinarySensorEntityDescription,
    describe,
)
from .const import SIGNAL_DISCOVERY
from .coordinator import (
    TraccarClientExtendedCoordinator,
    TraccarConfigEntry,
    TraccarDeviceData,
)
from .entity import TraccarClientExtendedEntity


def _online(data: TraccarDeviceData) -> bool | None:
    """Return the device's connectivity, or None while Traccar is unsure."""
    status = data["device"].get("status")
    if status in (None, "unknown"):
        return None
    return status == "online"


FIXED_BINARY_SENSORS: tuple[TraccarBinarySensorEntityDescription, ...] = (
    TraccarBinarySensorEntityDescription(
        key="online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_online,
    ),
    TraccarBinarySensorEntityDescription(
        key="position_valid",
        name="Position valid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data["position"].get("valid"),
    ),
    TraccarBinarySensorEntityDescription(
        key="position_outdated",
        name="Position outdated",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data["position"].get("outdated"),
    ),
    TraccarBinarySensorEntityDescription(
        key="device_disabled",
        name="Disabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data["device"].get("disabled"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TraccarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors, adding more as attributes appear."""
    coordinator = entry.runtime_data
    created: set[str] = set()

    @callback
    def _discover() -> None:
        by_subentry: dict[str, list[TraccarBinarySensor]] = {}
        for device_id, record in (coordinator.data or {}).items():
            device = record["device"]
            unique_prefix = device["uniqueId"]
            # A device only produces entities once it has a subentry, which is
            # what makes the device list opt-in.
            subentry_id = coordinator.subentry_id_for(unique_prefix)
            if subentry_id is None:
                continue
            overrides = coordinator.overrides_for(unique_prefix)

            descriptions: list[TraccarBinarySensorEntityDescription] = list(
                FIXED_BINARY_SENSORS
            )
            for key, value in coordinator.discoverable_attributes(device_id).items():
                description = describe(key, value, overrides.get(key))
                if isinstance(description, TraccarBinarySensorEntityDescription):
                    descriptions.append(description)

            for description in descriptions:
                unique_id = f"{unique_prefix}_{description.key}"
                if unique_id in created:
                    continue
                created.add(unique_id)
                by_subentry.setdefault(subentry_id, []).append(
                    TraccarBinarySensor(coordinator, device, description)
                )

        for subentry_id, entities in by_subentry.items():
            async_add_entities(entities, config_subentry_id=subentry_id)

    _discover()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discover))


class TraccarBinarySensor(TraccarClientExtendedEntity, BinarySensorEntity):
    """A Traccar binary sensor, from either a position field or an attribute."""

    entity_description: TraccarBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: TraccarClientExtendedCoordinator,
        device: DeviceModel,
        description: TraccarBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = f"{device['uniqueId']}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the state, applying inversion where the mapping needs it."""
        if not self.available:
            return None

        data = self.traccar_data
        if self.entity_description.value_fn is not None:
            value = self.entity_description.value_fn(data)
        elif (key := self.entity_description.attribute_key) is not None:
            value = data["attributes"].get(key)
        else:
            return None

        if value is None:
            return None

        # Traccar sometimes sends "true"/"false" strings rather than booleans.
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        else:
            value = bool(value)

        return not value if self.entity_description.invert else value
