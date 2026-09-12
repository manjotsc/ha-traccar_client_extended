"""Repair flows for Traccar Client Extended.

The one that matters creates the Traccar notifications that events depend on.
Traccar pushes an event to a websocket client only through `NotificatorWeb`,
which fires when a Notification with the `web` channel matches — so without one,
every event entity stays silent and nothing explains why. Reporting that is
useful; fixing it in a click is better, since the fix is a handful of API calls
the integration is already authenticated for.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    DOMAIN,
    EVENT_LABELS,
    EVENTS,
    ISSUE_NO_WEB_NOTIFICATION,
    LOGGER,
)

CONF_TYPES = "types"


class WebNotificationRepairFlow(RepairsFlow):
    """Offer to create the web notifications Traccar needs to push events."""

    def __init__(self, entry_id: str) -> None:
        """Remember which config entry raised the issue."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Skip straight to the choice; there is nothing to explain twice."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create a web notification for each chosen event type."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or not hasattr(entry, "runtime_data"):
            return self.async_abort(reason="entry_not_loaded")
        coordinator = entry.runtime_data

        if user_input is not None:
            types = user_input[CONF_TYPES]
            try:
                created = await coordinator.async_create_web_notifications(types)
            except Exception as err:  # noqa: BLE001 - surfaced to the user below
                LOGGER.error("Could not create Traccar notifications: %s", err)
                return self.async_abort(reason="create_failed")

            LOGGER.info("Created %s Traccar web notification(s)", created)
            # The check clears the issue itself once it sees them.
            await coordinator.async_check_event_delivery()
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TYPES, default=coordinator.wanted_event_types()
                    ): SelectSelector(
                        SelectSelectorConfig(
                            mode=SelectSelectorMode.DROPDOWN,
                            multiple=True,
                            sort=True,
                            # Every type Traccar can emit, all pre-ticked, so
                            # nothing is silently undeliverable. Untick what you
                            # do not want rather than working out which of 23
                            # notifications is the missing one.
                            options=[
                                {"value": event_type, "label": EVENT_LABELS[event_type]}
                                for event_type in sorted(EVENTS)
                            ],
                        )
                    )
                }
            ),
            description_placeholders={"name": entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Return the flow for a repairable issue."""
    if issue_id.startswith(ISSUE_NO_WEB_NOTIFICATION) and data:
        return WebNotificationRepairFlow(str(data["entry_id"]))
    raise ValueError(f"{DOMAIN} has no repair flow for {issue_id}")
