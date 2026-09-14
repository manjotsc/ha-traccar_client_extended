"""One sub-entry per device model, holding the devices of that model.

Home Assistant's integration page renders one card per config sub-entry, titled
with the sub-entry title, and nests that sub-entry's devices inside it. It does
not read `via_device`. So the only way to show

    FTC921
    ├── Cobalt
    ├── Malibu
    └── Uplander

is for FTC921 to *be* a sub-entry. A sub-entry per device, which is what this
started as, can only ever produce one card per device -- naming them
``FTC921 · Cobalt`` sorts them adjacent and no more.

The cost is that a sub-entry is also where settings live. Model-level is the
better home for most of them: a profile already describes a model, and so does
almost every attribute override, since `io800` means the same thing on every
FTC921. The genuinely per-device settings -- a name, an accuracy filter, the
odd override for one tracker -- live in a `devices` map inside the sub-entry
and are edited through a device picker.

Two consequences worth knowing:

* Deleting a model sub-entry deletes every device in it. That is Home
  Assistant's behaviour for sub-entries, not a choice made here.
* A device whose Traccar model changes has to move between sub-entries, taking
  its registry device and entities with it, or it would keep the old model's
  settings and sit under the wrong card. `async_sync_shape` does that.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_ATTRIBUTE_OVERRIDES,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_UNIQUE_ID,
    CONF_DEVICES,
    CONF_MAX_ACCURACY,
    CONF_MODEL,
    CONF_TRACCAR_DEVICE_ID,
    DOMAIN,
    LOGGER,
    SUBENTRY_TYPE_DEVICE,
)

_SLUG = re.compile(r"[^a-z0-9]+")

#: Title for the sub-entry collecting devices with no model set in Traccar.
#: Traccar's `model` is free text most people never fill in, and a card called
#: "" would read as a bug.
NO_MODEL_TITLE = "Unspecified model"


@callback
def model_key(model: str | None) -> str:
    """Return the sub-entry unique id for a model.

    Slugged and lowercased so `FTC921` and `ftc921 ` are one group, because
    profile matching compares models case-insensitively and a grouping that
    disagreed with it would split one fleet across two cards.
    """
    return f"model:{_SLUG.sub('_', (model or '').strip().lower()).strip('_')}"


@callback
def model_title(model: str | None, profiles: dict[str, Any]) -> str:
    """Title a model sub-entry, preferring the profile's wording.

    `FTC921` is what Traccar stores; `Teltonika FTC921` is what a person reads.
    """
    from .profiles import match

    if not (model := (model or "").strip()):
        return NO_MODEL_TITLE
    profile = match(profiles, model)
    return profile.display_name if profile else model


@callback
def device_entries(subentry: ConfigSubentry) -> dict[str, dict[str, Any]]:
    """Return a model sub-entry's per-device settings, keyed by uniqueId."""
    devices = subentry.data.get(CONF_DEVICES)
    return dict(devices) if isinstance(devices, dict) else {}


@callback
def find_subentry(entry: ConfigEntry, model: str | None) -> ConfigSubentry | None:
    """Return the sub-entry covering a model, if one exists."""
    wanted = model_key(model)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_DEVICE and (
            subentry.unique_id == wanted
        ):
            return subentry
    return None


@callback
def new_subentry(
    model: str | None, profiles: dict[str, Any], devices: dict[str, dict[str, Any]]
) -> ConfigSubentry:
    """Build a model sub-entry holding the given devices."""
    return ConfigSubentry(
        data={CONF_MODEL: (model or "").strip(), CONF_DEVICES: devices},
        subentry_type=SUBENTRY_TYPE_DEVICE,
        title=model_title(model, profiles),
        unique_id=model_key(model),
    )


@callback
def device_settings(name: str, traccar_id: int | None) -> dict[str, Any]:
    """Build the per-device entry that goes inside a model sub-entry."""
    return {CONF_DEVICE_NAME: name, CONF_TRACCAR_DEVICE_ID: traccar_id}


@callback
def with_device(
    subentry: ConfigSubentry, unique_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """Return the sub-entry's data with one device added or replaced."""
    devices = device_entries(subentry)
    devices[unique_id] = settings
    return {**subentry.data, CONF_DEVICES: devices}


@callback
def without_device(subentry: ConfigSubentry, unique_id: str) -> dict[str, Any]:
    """Return the sub-entry's data with one device removed."""
    devices = device_entries(subentry)
    devices.pop(unique_id, None)
    return {**subentry.data, CONF_DEVICES: devices}


# -- Keeping the shape right --------------------------------------------------


@callback
def async_sync_shape(
    hass: HomeAssistant,
    entry: ConfigEntry,
    models: dict[str, str | None],
    profiles: dict[str, Any],
) -> None:
    """Fold per-device sub-entries into model ones, and move what has moved.

    Called once at setup with ``{device unique id: Traccar model}`` for every
    device this entry knows about. It does three things, in an order that
    matters:

    1. Convert any sub-entry written by the per-device version into the model
       sub-entry its device belongs to, carrying that device's settings over.
    2. Move a device whose model has changed since it was last seen.
    3. Remove a model sub-entry left holding nothing.

    Registry entries are reassigned rather than recreated. Removing a sub-entry
    deletes its devices and entities, so doing this the lazy way would cost
    every entity id, customisation and scrap of history in the recorder.
    """
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        if (legacy := subentry.data.get(CONF_DEVICE_UNIQUE_ID)) is not None:
            try:
                _absorb_legacy(hass, entry, subentry, legacy, models, profiles)
            except Exception:  # noqa: BLE001 - the rest of the fleet still migrates
                LOGGER.exception("Could not migrate the sub-entry for %s", legacy)

    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        for unique_id in list(device_entries(subentry)):
            if unique_id not in models:
                # Traccar no longer reports it. Left alone: the device may be
                # temporarily absent, and deciding it is gone is not ours.
                continue
            if subentry.unique_id != model_key(models[unique_id]):
                _move_device(hass, entry, subentry, unique_id, models, profiles)

    _prune_empty(hass, entry)


@callback
def _absorb_legacy(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    unique_id: str,
    models: dict[str, str | None],
    profiles: dict[str, Any],
) -> None:
    """Fold one per-device sub-entry into the model sub-entry for its device."""
    model = models.get(unique_id, subentry.data.get(CONF_MODEL))
    settings = device_settings(
        subentry.data.get(CONF_DEVICE_NAME) or unique_id,
        subentry.data.get(CONF_TRACCAR_DEVICE_ID),
    )
    # The settings that were per device stay per device. A profile choice does
    # not: it describes a model, and the sub-entry is now the model -- so it is
    # carried up to the model rather than dropped, which silently undid a
    # deliberate "use this profile" or "use no profile" on every upgrade.
    for key in (CONF_ATTRIBUTE_OVERRIDES, CONF_MAX_ACCURACY):
        if (value := subentry.data.get(key)) not in (None, [], ""):
            settings[key] = value
    profile = subentry.data.get(CONF_DEVICE_PROFILE)

    target = _target_for(
        hass, entry, model, profiles, unique_id, settings, profile=profile
    )
    if target is None:
        # Removing the old sub-entry now would delete the device and every
        # entity on it, and `known_devices` would stop auto-add putting it
        # back -- so the device would be gone for good. Leaving the old
        # sub-entry in place costs a stray card and nothing else, and the next
        # setup tries again.
        LOGGER.error(
            "Could not file %s under model %r; leaving it as it was",
            unique_id,
            model,
        )
        return

    if not _reassign(hass, entry, unique_id, subentry.subentry_id, target.subentry_id):
        return
    LOGGER.info("Folded %s into the %s sub-entry", unique_id, model or "unspecified")
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)


@callback
def _move_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    source: ConfigSubentry,
    unique_id: str,
    models: dict[str, str | None],
    profiles: dict[str, Any],
) -> None:
    """Move one device to the sub-entry for its current model."""
    model = models[unique_id]
    settings = device_entries(source)[unique_id]

    target = _target_for(hass, entry, model, profiles, unique_id, settings)
    if target is None:
        LOGGER.error("Could not move %s to model %r; leaving it", unique_id, model)
        return

    # Read back rather than reused: the call above replaced the object, and the
    # stale one still lists the device we are about to take out of it.
    if (current := entry.subentries.get(source.subentry_id)) is not None:
        hass.config_entries.async_update_subentry(
            entry, current, data=without_device(current, unique_id)
        )
    _reassign(hass, entry, unique_id, source.subentry_id, target.subentry_id)
    LOGGER.info(
        "Device %s moved to the %s sub-entry", unique_id, model or "unspecified"
    )


@callback
def _target_for(
    hass: HomeAssistant,
    entry: ConfigEntry,
    model: str | None,
    profiles: dict[str, Any],
    unique_id: str,
    settings: dict[str, Any],
    profile: str | None = None,
) -> ConfigSubentry | None:
    """Return the model sub-entry holding this device, creating it if needed.

    `profile` seeds a newly created sub-entry with the choice the device was
    carrying. It is only ever a seed: an existing card keeps its own, because
    the first device of a model to migrate should not overrule what the rest
    of the fleet already agreed on.

    `None` means something went wrong and the caller must not go on to delete
    anything: removing a sub-entry takes its devices and entities with it.
    """
    if (target := find_subentry(entry, model)) is not None:
        hass.config_entries.async_update_subentry(
            entry, target, data=with_device(target, unique_id, settings)
        )
        return entry.subentries.get(target.subentry_id, target)

    fresh = new_subentry(model, profiles, {unique_id: settings})
    if profile:
        fresh = ConfigSubentry(
            data={**fresh.data, CONF_DEVICE_PROFILE: profile},
            subentry_id=fresh.subentry_id,
            subentry_type=fresh.subentry_type,
            title=fresh.title,
            unique_id=fresh.unique_id,
        )

    try:
        hass.config_entries.async_add_subentry(entry, fresh)
    except Exception:  # noqa: BLE001 - one bad fold must not fail setup
        LOGGER.exception("Could not create a sub-entry for model %r", model)
        return None
    return find_subentry(entry, model)


@callback
def _reassign(
    hass: HomeAssistant, entry: ConfigEntry, unique_id: str, source: str, target: str
) -> bool:
    """Point a device's registry entry and entities at another sub-entry.

    Returns False when something is still attached to the old sub-entry, which
    means the caller must not remove it: `async_remove_subentry` calls
    `async_clear_config_subentry`, and anything left on it is deleted with it.
    An empty sub-entry returns True -- there is nothing to lose.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    device = devices.async_get_device(identifiers={(DOMAIN, unique_id)})
    if device is not None:
        devices.async_update_device(
            device.id,
            add_config_subentry_id=target,
            add_config_entry_id=entry.entry_id,
        )

    # Every entity of the old sub-entry, whether or not a device was found.
    # Matching on the device id as well used to skip any entity whose device
    # the registry could not resolve, and those were then deleted along with
    # the sub-entry.
    moved = 0
    for registered in er.async_entries_for_config_entry(entities, entry.entry_id):
        if registered.config_subentry_id == source:
            entities.async_update_entity(
                registered.entity_id, config_subentry_id=target
            )
            moved += 1

    if device is not None:
        devices.async_update_device(device.id, remove_config_subentry_id=source)

    left = [
        registered.entity_id
        for registered in er.async_entries_for_config_entry(entities, entry.entry_id)
        if registered.config_subentry_id == source
    ]
    if left:
        LOGGER.error(
            "%s still has %s entity(s) on the old sub-entry; not removing it",
            unique_id,
            len(left),
        )
        return False

    LOGGER.debug(
        "Moved %s: device %s, %s entity(s)",
        unique_id,
        "found" if device else "none in the registry",
        moved,
    )
    return True


@callback
def _prune_empty(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove model sub-entries left holding no devices.

    The last device of a model changing model leaves an empty card behind.
    Nothing is lost with it: the settings it carried described devices that are
    no longer there.
    """
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        if CONF_DEVICE_UNIQUE_ID in subentry.data:
            continue
        if not device_entries(subentry):
            LOGGER.debug("Removing empty %s sub-entry", subentry.title)
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
