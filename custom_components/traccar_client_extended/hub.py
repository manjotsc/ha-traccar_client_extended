"""The hub device: one per Traccar server, and every tracker connects through it.

`via_device` means "connects through", which is literally true of the server, so
it shows as "Connected via" on each device's page along with the server's
configuration link.

**It does not group anything on the integration page.** That page renders one
card per *config sub-entry*, titled with the sub-entry title, and sweeps
anything without one into "Devices that don't belong to a sub-entry". It does
not read `via_device` at all.

A device per model, with each tracker hanging off it, was built on the
assumption that it did. The result was three service devices sitting in that
catch-all bucket and no grouping whatsoever, so it is gone and
`_prune_model_groups` removes the devices it left behind. Grouping is done
where that page actually looks: one sub-entry *per model*, which is what
`subentries` builds.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, LOGGER
from .coordinator import TraccarClientExtendedCoordinator


@callback
def async_setup_devices(
    hass: HomeAssistant, entry_id: str, coordinator: TraccarClientExtendedCoordinator
) -> None:
    """Register the server hub.

    Called before platforms are forwarded. A device naming a `via_device` that
    does not exist yet is dropped by the registry, and the tracker then sits at
    the top level with no way back short of deleting it.
    """
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={coordinator.hub_identifier},
        name=coordinator.server_host,
        manufacturer="Traccar",
        model="Traccar Server",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=coordinator.server_url,
    )

    # Model grouping was briefly a device per model, with each tracker hanging
    # off it. It produced nothing on the integration page -- see the module
    # docstring -- so the groups are removed rather than left as clutter in
    # anyone's registry who ran that version.
    _prune_model_groups(registry, entry_id)


@callback
def _prune_model_groups(registry: dr.DeviceRegistry, entry_id: str) -> None:
    """Remove the per-model devices an earlier version created.

    Only devices whose identifier that version minted are considered, so a
    tracker is never at risk.
    """
    prefix = f"model_{entry_id}_"
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if any(
            identifier[0] == DOMAIN and identifier[1].startswith(prefix)
            for identifier in device.identifiers
        ):
            LOGGER.debug("Removing obsolete model group device %s", device.name)
            registry.async_remove_device(device.id)
