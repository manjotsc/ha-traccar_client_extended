"""The Traccar Client Extended integration."""

from __future__ import annotations

from aiohttp import CookieJar
from pytraccar import ApiClient

from homeassistant.const import (
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.loader import async_get_loaded_integration

from .const import DEFAULT_PORT, DOMAIN, LOGGER
from .coordinator import TraccarClientExtendedCoordinator, TraccarConfigEntry
from .hub import async_setup_devices
from .operators import async_get_operators
from .profiles import async_get_profiles
from .services import async_register_services
from .subentries import async_sync_shape

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: TraccarConfigEntry) -> bool:
    """Set up Traccar Client Extended from a config entry."""
    # Logged so a report can be tied to an exact build. Extracting the release
    # zip one directory too deep leaves the previous version in place and
    # silently running, which is otherwise very hard to spot.
    LOGGER.debug(
        "Setting up %s", async_get_loaded_integration(hass, entry.domain).version
    )

    # A dedicated session with its own cookie jar, for two reasons.
    #
    # Traccar authenticates REST calls with a bearer token but authenticates
    # /api/socket with a JSESSIONID cookie obtained from /api/session. aiohttp's
    # default jar silently refuses cookies from bare IP hosts, which is exactly
    # how most people reach Traccar, so the jar has to be unsafe or the
    # websocket connects unauthenticated and is closed by the server.
    #
    # It must also not be Home Assistant's shared session, or that session
    # cookie would leak into a jar every other integration uses.
    #
    # `unsafe` unconditionally, not only for plain HTTP. It was gated on the
    # connection being unencrypted or unverified, which put back the very
    # failure the paragraph above describes for anyone reaching a verified
    # HTTPS endpoint by IP address: REST keeps working, the websocket is closed
    # for being unauthenticated, and nothing anywhere says why. The gate bought
    # nothing either -- this jar is created per config entry and only ever sees
    # one host, so "unsafe" here means no more than storing a cookie for an
    # address rather than a name.
    client_session = async_create_clientsession(hass, cookie_jar=CookieJar(unsafe=True))

    coordinator = TraccarClientExtendedCoordinator(
        hass=hass,
        config_entry=entry,
        session=client_session,
        client=ApiClient(
            client_session=client_session,
            host=entry.data[CONF_HOST],
            port=entry.data.get(CONF_PORT, DEFAULT_PORT),
            token=entry.data[CONF_API_TOKEN],
            ssl=entry.data[CONF_SSL],
            verify_ssl=entry.data[CONF_VERIFY_SSL],
        ),
    )

    async_register_services(hass)

    coordinator.profiles = await async_get_profiles(hass)
    coordinator.operators = await async_get_operators(hass)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # After the first refresh, because the model groups come from live device
    # records; before the platforms, because a device naming a `via_device`
    # that does not exist yet is dropped from the registry and the tracker then
    # sits at the top level with no way back short of deleting it.
    async_setup_devices(hass, entry.entry_id, coordinator)
    # Folds an install written by the per-device version into model sub-entries
    # and moves anything whose model changed, carrying registry entries with it.
    async_sync_shape(hass, entry, coordinator.device_models(), coordinator.profiles)
    # It rewrites the devices map inside sub-entries, which the cached lookup
    # keys cannot see.
    coordinator.invalidate_subentry_index()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Traccar pushes events only when a matching web notification exists, which
    # is invisible from this side until nothing ever arrives.
    entry.async_create_background_task(
        hass=hass,
        target=coordinator.async_check_event_delivery(),
        name=f"{DOMAIN} event delivery check",
    )

    entry.async_create_background_task(
        hass=hass,
        target=coordinator.subscribe(),
        name=f"{DOMAIN} subscription",
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TraccarConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: TraccarConfigEntry) -> bool:
    """Migrate an old config entry.

    Nothing to migrate yet; present so a future schema bump is cheap.
    """
    return True
