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
from .profiles import async_get_profiles
from .services import async_register_services

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
    client_session = async_create_clientsession(
        hass,
        cookie_jar=CookieJar(
            unsafe=not entry.data[CONF_SSL] or not entry.data[CONF_VERIFY_SSL]
        ),
    )

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
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

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
