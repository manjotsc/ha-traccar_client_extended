"""Put a device's entities back to the enabled state their mapping asks for.

The inverse of "switch them all on" is not "switch them all off" -- that would
take out the device tracker and every real reading, leaving a device that
reports nothing, which nobody wants a button for. The useful undo is *back to
defaults*: the diagnostics go off again and the readings stay on.

Nothing is stored to make that possible. The default for an entity is already
carried by its description as ``entity_registry_enabled_default``, so it is
recomputed from live data at the moment of the reset. A list written down when
"enable all" ran would be a second source of truth, stale the moment anybody
used Home Assistant's own entity settings, and it would not follow a profile
change either.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .attributes import describe, describe_fields
from .binary_sensor import FIXED_BINARY_SENSORS
from .const import LOGGER
from .coordinator import TraccarClientExtendedCoordinator
from .sensor import FIXED_SENSORS


@callback
def enabled_defaults(
    coordinator: TraccarClientExtendedCoordinator, device_unique_id: str
) -> dict[str, bool]:
    """Return ``{unique_id: enabled by default}`` for one device's entities.

    Built the way discovery builds it, from the same descriptions, so a profile
    or an override that changes an entity's default is reflected here without
    this module knowing anything about either.
    """
    record = coordinator.record_for(device_unique_id)
    if record is None:
        return {}
    device_id = record["device"]["id"]

    descriptions = [*FIXED_SENSORS, *FIXED_BINARY_SENSORS]
    overrides = coordinator.overrides_for(device_unique_id)
    for key, value in coordinator.discoverable_attributes(device_id).items():
        override = overrides.get(key)
        if (description := describe(key, value, override)) is not None:
            descriptions.append(description)
        descriptions.extend(describe_fields(key, override))

    return {
        f"{device_unique_id}_{description.key}": (
            description.entity_registry_enabled_default
        )
        for description in descriptions
    }


@callback
def reset_subentry_entities(
    hass: HomeAssistant,
    coordinator: TraccarClientExtendedCoordinator,
    entry_id: str,
    subentry_id: str,
    device_unique_id: str,
) -> int:
    """Restore this device's entities to their default state. Returns changes.

    An entity with no description is left exactly as it is. That happens when
    the device has stopped reporting the attribute behind it, and deciding a
    reading is gone is the one judgement this integration does not make.
    """
    defaults = enabled_defaults(coordinator, device_unique_id)
    if not defaults:
        return 0

    registry = er.async_get(hass)
    changed = 0
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.config_subentry_id != subentry_id:
            continue
        if (wanted := defaults.get(entity.unique_id)) is None:
            continue

        # Only INTEGRATION and USER are ours to undo, for the same reason
        # `enable_subentry_entities` leaves the others alone: they mean
        # something larger is switched off.
        if wanted and entity.disabled_by in (
            er.RegistryEntryDisabler.INTEGRATION,
            er.RegistryEntryDisabler.USER,
        ):
            registry.async_update_entity(entity.entity_id, disabled_by=None)
            changed += 1
        elif not wanted and entity.disabled_by is None:
            registry.async_update_entity(
                entity.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            changed += 1

    LOGGER.debug("Reset %s entity(s) to their default enabled state", changed)
    return changed
