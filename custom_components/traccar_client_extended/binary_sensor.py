"""Binary sensor platform for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

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
    describe_fields,
    extract_bits,
)
from .const import SIGNAL_DISCOVERY
from .coordinator import (
    TraccarClientExtendedCoordinator,
    TraccarConfigEntry,
    TraccarDeviceData,
)
from .entity import TraccarClientExtendedEntity


def _online(data: TraccarDeviceData) -> bool | None:
    """Return whether Traccar currently has the device connected.

    Traccar has three statuses, and only one of them means connected:

    * `online`   -- reporting.
    * `offline`  -- the connection closed and the protocol could tell. Only
      connection-oriented decoders report this (`deviceDisconnected` acts on
      `supportsOffline`), so plenty of fleets never see it.
    * `unknown`  -- nothing heard for `status.timeout`, and the session was
      dropped by the sweep in `ConnectionManager`. This is the ordinary resting
      state of a tracker that has stopped reporting.

    `unknown` used to map to None here, on the reasoning that Home Assistant
    should not collapse a distinction Traccar draws. That was wrong in practice:
    it is the state most devices sit in once they go quiet, so the sensor read
    "Unknown" for a fleet parked overnight, automations on `off` never fired,
    and it looked like the integration had lost track. Traccar is not saying it
    does not know -- it is saying the device is not connected.

    The raw status is still published as an attribute, so the three-way
    distinction is there for anyone who wants it.
    """
    status = data["device"].get("status")
    if status is None:
        return None
    return status == "online"


FIXED_BINARY_SENSORS: tuple[TraccarBinarySensorEntityDescription, ...] = (
    TraccarBinarySensorEntityDescription(
        key="online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_online,
        attributes_fn=lambda data: {
            "traccar_status": data["device"].get("status"),
        },
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
                override = overrides.get(key)
                description = describe(key, value, override)
                if isinstance(description, TraccarBinarySensorEntityDescription):
                    descriptions.append(description)
                # A flag packed inside a status word is a binary sensor of its
                # own; the sensor platform takes the numeric fields.
                descriptions.extend(
                    field
                    for field in describe_fields(key, override)
                    if isinstance(field, TraccarBinarySensorEntityDescription)
                )

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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Publish anything the mapping had to flatten out of the state."""
        if (fn := self.entity_description.attributes_fn) is None:
            return None
        return fn(self.traccar_data)

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
            # A flag packed inside a status word rather than sent on its own.
            value = extract_bits(
                value,
                self.entity_description.bit_offset,
                self.entity_description.bit_width,
            )
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
