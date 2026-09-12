"""System health for Traccar Client Extended."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system health callback."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Report on each configured server.

    Reachability and subscription state are reported separately on purpose: the
    integration's nastiest failure is REST working while the websocket does not,
    and a single "connected" flag would hide exactly that.
    """
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    info: dict[str, Any] = {"configured_servers": len(entries)}

    for entry in entries:
        coordinator = entry.runtime_data
        info[f"{entry.title} REST"] = system_health.async_check_can_reach_url(
            hass, f"{coordinator.server_url}/api/server"
        )
        info[f"{entry.title} websocket"] = (
            "ok"
            if not coordinator.consecutive_subscription_failures
            else f"failing ({coordinator.consecutive_subscription_failures} attempts)"
        )
        info[f"{entry.title} devices"] = len(coordinator.data or {})

    return info
