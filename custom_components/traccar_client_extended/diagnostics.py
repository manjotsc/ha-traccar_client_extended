"""Diagnostics for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_TOKEN, CONF_HOST
from homeassistant.core import HomeAssistant

from .attributes import describe, is_known
from .coordinator import TraccarConfigEntry

TO_REDACT = {
    CONF_API_TOKEN,
    CONF_HOST,
    "address",
    "contact",
    "geofenceIds",
    "iccid",
    "ip",
    "latitude",
    "longitude",
    "network",
    "phone",
    "uniqueId",
    "vin",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TraccarConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    The attribute inventory is the useful part: it shows which keys each device
    reports and how this integration classified them, which is what a bug report
    about a missing or mistyped entity needs.
    """
    coordinator = entry.runtime_data

    inventory: dict[str, Any] = {}
    for device_id, record in (coordinator.data or {}).items():
        # Resolve through the same layering the platforms use, or this reports
        # the built-in mapping for an attribute a profile has reinterpreted.
        overrides = coordinator.overrides_for(record["device"].get("uniqueId"))
        profile = coordinator.profile_for(record["device"].get("uniqueId"))
        classified: dict[str, Any] = {}
        for key, value in record["attributes"].items():
            description = describe(key, value, overrides.get(key))
            classified[key] = {
                "python_type": type(value).__name__,
                "curated": is_known(key),
                "overridden": key in overrides,
                "entity": None if description is None else description.key,
                "device_class": (
                    None
                    if description is None
                    else str(getattr(description, "device_class", None))
                ),
                "unit": (
                    None
                    if description is None
                    else getattr(description, "native_unit_of_measurement", None)
                ),
            }
        inventory[str(device_id)] = {
            "profile": profile.name if profile else None,
            "attributes": classified,
            "discoverable": sorted(coordinator.discoverable_attributes(device_id)),
        }

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "max_accuracy": coordinator.max_accuracy,
            "discover_unknown": coordinator.discover_unknown,
            "max_discovered": coordinator.max_discovered,
            "events": coordinator.events,
            "device_count": len(coordinator.data or {}),
        },
        # How long positions take to reach us after Traccar has them, which is
        # the first thing to look at in any report about lag. A median over two
        # seconds with a small spread is Traccar's own buffering rather than
        # anything here; see the repair of the same name.
        "delivery_latency": coordinator.latency_summary(),
        "attribute_inventory": inventory,
        "devices": async_redact_data(
            [
                {
                    "device": record["device"],
                    "position": record["position"],
                    "geofences": [g["name"] for g in record["geofences"]],
                }
                for record in (coordinator.data or {}).values()
            ],
            TO_REDACT,
        ),
    }
