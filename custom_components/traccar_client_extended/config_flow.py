"""Config flow for Traccar Client Extended."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import aiohttp
import voluptuous as vol
from aiohttp import CookieJar
from pytraccar import (
    ApiClient,
    ServerModel,
    TraccarAuthenticationException,
    TraccarException,
)

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_API_TOKEN,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
    SchemaOptionsFlowHandlerWithReload,
)
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .attributes import (
    DENYLIST,
    OVERRIDE_ATTRIBUTE,
    OVERRIDE_DEVICE_CLASS,
    OVERRIDE_SCALE,
    OVERRIDE_TREAT_AS,
    OVERRIDE_UNIT,
    allowed_units,
    check_device_class_unit,
    override_rows,
)
from .const import (
    CONF_ATTRIBUTE_OVERRIDES,
    CONF_AUTO_ADD_DEVICES,
    CONF_CONFIRM,
    CONF_CREATE_NOTIFICATIONS,
    CONF_CUSTOM_ATTRIBUTES,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_UNIQUE_ID,
    CONF_DEVICES,
    CONF_DISCOVER_UNKNOWN,
    CONF_EDIT_DEVICE,
    CONF_ENABLE_ALL_ENTITIES,
    CONF_EVENTS,
    CONF_IGNORED_ATTRIBUTES,
    CONF_KNOWN_DEVICES,
    CONF_MAX_ACCURACY,
    CONF_MAX_DISCOVERED,
    CONF_MODEL,
    CONF_RESET_ENTITIES,
    CONF_SKIP_ACCURACY_FILTER_FOR,
    DEFAULT_AUTO_ADD_DEVICES,
    DEFAULT_CREATE_NOTIFICATIONS,
    DEFAULT_DISCOVER_UNKNOWN,
    DEFAULT_MAX_ACCURACY,
    DEFAULT_MAX_DISCOVERED,
    DEFAULT_PORT,
    DOMAIN,
    EVENT_LABELS,
    EVENTS,
    LOGGER,
    PROFILE_AUTO,
    PROFILE_NONE,
    SUBENTRY_TYPE_DEVICE,
)
from .entity_defaults import reset_subentry_entities
from .profiles import match, suggest
from .subentries import (
    device_entries,
    device_settings,
    find_subentry,
    model_key,
    model_title,
    with_device,
)

#: How long a token minted from an email and password is asked to last.
#: Traccar's own default is 7 days (`TokenManager.DEFAULT_EXPIRATION_DAYS`),
#: which suits a browser session and not an integration: it would expire in a
#: week and reappear as a reauth prompt nobody could explain. Traccar caps the
#: value only by the session's own expiry, so ask for something long and let it
#: clamp. Revoking is `POST /api/session/token/revoke`, or delete the token in
#: Traccar under Settings -> Account.
TOKEN_EXPIRY = timedelta(days=3650)


def _clean_port(value: Any) -> int:
    """Return the port as an integer, raising `vol.Invalid` if it is not one.

    The field is a `TextSelector` rather than a `NumberSelector` because the
    number control renders 8082 as "8,082", so nothing validates it and
    whatever was typed is stored verbatim. An empty box then produces
    `http://host:/api` -- which is what `server_url` hands the notification
    check and every device's configuration link -- while pytraccar quietly
    falls back to 8082 for its own base url, so REST works and nothing else
    does. Coerced here, so what reaches the entry is always an int.
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise vol.Invalid("invalid_port") from None
    if not 1 <= port <= 65535:
        raise vol.Invalid("invalid_port")
    return port


def _overrides_selector(seen: Sequence[str], *, allow_ignore: bool) -> ObjectSelector:
    """Build the attribute overrides table.

    This was a text box parsed by hand -- `io800: voltage, V, x0.001` -- which
    required knowing a grammar, the exact attribute key, and that the device
    class is spelled `voltage`. Every column is now a control, and the two
    fields that used to be guesses are lists.

    `allow_ignore` is false server-wide, where the picker above is the control
    for turning an attribute off, and true per device, where there is no picker
    and the table is the only more-specific layer.

    Five columns, not seven. `AttributeOverride` also carries a display name and
    a decimal precision, but Home Assistant already offers both per entity in
    its own settings, and duplicating them here bought a second place to set the
    same thing at the cost of a row too cramped to read. Profiles still set them
    -- a contributor describing hardware in JSON has no entity to configure.
    """
    return ObjectSelector(
        ObjectSelectorConfig(
            multiple=True,
            label_field=OVERRIDE_ATTRIBUTE,
            description_field=OVERRIDE_DEVICE_CLASS,
            fields={
                OVERRIDE_ATTRIBUTE: {
                    "label": "Attribute",
                    "required": True,
                    # Free text allowed: a device that is offline right now
                    # still deserves an override, and its keys are not in the
                    # list until it reports.
                    "selector": SelectSelector(
                        SelectSelectorConfig(
                            mode=SelectSelectorMode.DROPDOWN,
                            options=list(seen),
                            custom_value=True,
                            sort=True,
                        )
                    ),
                },
                OVERRIDE_TREAT_AS: {
                    "label": "Treat as",
                    "selector": SelectSelector(
                        SelectSelectorConfig(
                            mode=SelectSelectorMode.DROPDOWN,
                            options=(
                                ["sensor", "binary_sensor", "ignore"]
                                if allow_ignore
                                else ["sensor", "binary_sensor"]
                            ),
                            translation_key="override_treat_as",
                        )
                    ),
                },
                OVERRIDE_DEVICE_CLASS: {
                    "label": "Device class (sensors)",
                    "selector": SelectSelector(
                        SelectSelectorConfig(
                            mode=SelectSelectorMode.DROPDOWN,
                            options=sorted(item.value for item in SensorDeviceClass),
                            sort=True,
                        )
                    ),
                },
                OVERRIDE_UNIT: {
                    "label": "Unit",
                    "selector": TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                },
                OVERRIDE_SCALE: {
                    "label": "Multiply by",
                    "selector": NumberSelector(
                        NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
                    ),
                },
            },
        )
    )


def check_override_rows(
    rows: Sequence[Mapping[str, Any]], ignored: Sequence[str] = ()
) -> str | None:
    """Return an error key if these rows would not do what they appear to.

    Two ways that happens. A device class paired with a unit Home Assistant does
    not accept is stored without complaint and then quietly excluded from
    long-term statistics. And an attribute that is both ticked above and given a
    row here is asked to be two things at once: the picker and this table are
    the same layer, so the row wins and the tick does nothing. Rather than pick
    that winner silently, say so.
    """
    ignored_set = set(ignored)
    for row in rows:
        attribute = row.get(OVERRIDE_ATTRIBUTE)
        if attribute in ignored_set:
            LOGGER.warning(
                "Attribute %s is both ignored and given an override row; the "
                "row would win and the tick would do nothing",
                attribute,
            )
            return "attribute_ignored_and_overridden"

        device_class = row.get(OVERRIDE_DEVICE_CLASS)
        if error := check_device_class_unit(device_class, row.get(OVERRIDE_UNIT)):
            LOGGER.warning(
                "Attribute %s: device class '%s' with unit %r is not a pairing "
                "Home Assistant accepts; valid units are %s",
                attribute,
                device_class,
                row.get(OVERRIDE_UNIT),
                allowed_units(device_class) or "none",
            )
            return error
    return None


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_PORT, default=str(DEFAULT_PORT)): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_SSL, default=False): BooleanSelector(),
        vol.Optional(CONF_VERIFY_SSL, default=True): BooleanSelector(),
        vol.Optional(
            CONF_CREATE_NOTIFICATIONS, default=DEFAULT_CREATE_NOTIFICATIONS
        ): BooleanSelector(),
    }
)

# Signing in mints a token; the password is used once and never stored, so what
# ends up in the config entry is identical either way.
STEP_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_PORT, default=str(DEFAULT_PORT)): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_SSL, default=False): BooleanSelector(),
        vol.Optional(CONF_VERIFY_SSL, default=True): BooleanSelector(),
        vol.Optional(
            CONF_CREATE_NOTIFICATIONS, default=DEFAULT_CREATE_NOTIFICATIONS
        ): BooleanSelector(),
    }
)

_FREE_TEXT_LIST = SelectSelector(
    SelectSelectorConfig(
        mode=SelectSelectorMode.DROPDOWN,
        multiple=True,
        sort=True,
        custom_value=True,
        options=[],
    )
)


async def _save_devices(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Save the device settings, and re-add deleted devices if that was ticked.

    One step rather than two: both answer "which devices exist". The tickbox is
    an action rather than a setting, so leaving it clear saves the rest and does
    nothing, and it is deliberately not returned -- storing it would fire the
    re-add again on the next unrelated save of this step.
    """
    if user_input.get(CONF_CONFIRM):
        # Deleting a device records it as already seen, which is what stops
        # auto-add putting it straight back. Clearing that record undoes a
        # deletion in bulk rather than re-adding devices one at a time.
        entry = handler.parent_handler.config_entry
        hass = handler.parent_handler.hass
        hass.config_entries.async_update_entry(
            entry,
            data={k: v for k, v in entry.data.items() if k != CONF_KNOWN_DEVICES},
        )
        # This changed entry.data, not options, so the reloading options handler
        # may see nothing to do. Reload explicitly rather than wait for the next
        # full refresh.
        hass.config_entries.async_schedule_reload(entry.entry_id)

    return {
        CONF_AUTO_ADD_DEVICES: user_input.get(
            CONF_AUTO_ADD_DEVICES, DEFAULT_AUTO_ADD_DEVICES
        )
    }


# One menu entry per thing someone actually opens this dialog to change.
#
# A single "Settings" step held nine unrelated options behind a label that said
# nothing, so changing the accuracy filter meant scrolling past the attribute
# override DSL. Each step writes only its own keys -- SchemaCommonFlowHandler
# merges into the stored options rather than replacing them -- so splitting the
# form does not disturb settings owned by another step, and the data stays flat.
async def _attributes_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Build the attributes step from what this server actually reports.

    The picker's options cannot be a fixed list: what a device reports depends
    on its hardware and firmware, and an unmapped Teltonika sends raw `ioNNN`
    keys nobody could enumerate in advance. Turning one off used to mean typing
    `io800: ignore` into a text box, which required knowing both the syntax and
    the exact key -- and the key is only discoverable by reading diagnostics.
    """
    entry = handler.parent_handler.config_entry
    coordinator = getattr(entry, "runtime_data", None)
    seen = coordinator.seen_attribute_keys() if coordinator is not None else []
    # Anything already ignored stays selectable even if the device that reported
    # it is offline or gone. Without this the selector would reject the stored
    # value and saving the form would quietly re-enable the attribute.
    stored = entry.options.get(CONF_IGNORED_ATTRIBUTES) or []
    stored_rows = override_rows(entry.options.get(CONF_ATTRIBUTE_OVERRIDES))

    return vol.Schema(
        {
            vol.Optional(CONF_IGNORED_ATTRIBUTES, default=[]): SelectSelector(
                SelectSelectorConfig(
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                    sort=True,
                    custom_value=True,
                    options=sorted(set(seen) | set(stored)),
                )
            ),
            vol.Optional(
                CONF_DISCOVER_UNKNOWN, default=DEFAULT_DISCOVER_UNKNOWN
            ): BooleanSelector(),
            vol.Optional(
                CONF_MAX_DISCOVERED, default=DEFAULT_MAX_DISCOVERED
            ): NumberSelector(
                NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=0, max=200)
            ),
            vol.Optional(CONF_CUSTOM_ATTRIBUTES, default=[]): _FREE_TEXT_LIST,
            vol.Optional(CONF_ATTRIBUTE_OVERRIDES, default=[]): _overrides_selector(
                sorted(set(seen) | {row[OVERRIDE_ATTRIBUTE] for row in stored_rows}),
                allow_ignore=False,
            ),
        }
    )


async def _attributes_suggested(handler: SchemaCommonFlowHandler) -> dict[str, Any]:
    """Fill the form, converting overrides stored as text before the table.

    Without this the selector is handed a string, rejects it, and the entry
    silently loses every override it had the first time the form is opened.
    """
    options = dict(handler.options)
    options[CONF_ATTRIBUTE_OVERRIDES] = override_rows(
        options.get(CONF_ATTRIBUTE_OVERRIDES)
    )
    return options


async def _validate_attributes(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Refuse a device class and unit Home Assistant would not pair."""
    error = check_override_rows(
        user_input.get(CONF_ATTRIBUTE_OVERRIDES) or [],
        user_input.get(CONF_IGNORED_ATTRIBUTES) or [],
    )
    if error:
        raise SchemaFlowError(error)
    return user_input


OPTIONS_FLOW = {
    "init": SchemaFlowMenuStep(["events", "position", "attributes", "devices"]),
    "events": SchemaFlowFormStep(
        schema=vol.Schema(
            {
                vol.Optional(CONF_EVENTS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        mode=SelectSelectorMode.DROPDOWN,
                        multiple=True,
                        sort=True,
                        custom_value=False,
                        # Traccar's own wording, carried as labels rather than a
                        # translation_key: the stored value must stay Traccar's
                        # own `deviceOverspeed` because that is what the
                        # notification API takes, and Home Assistant requires a
                        # translation key to match [a-z0-9-_]+, which camelCase
                        # does not. Without the labels the picker would list raw
                        # API strings nobody has seen in Traccar.
                        options=[
                            {"value": event_type, "label": EVENT_LABELS[event_type]}
                            for event_type in sorted(EVENTS)
                        ],
                    )
                ),
            }
        )
    ),
    "position": SchemaFlowFormStep(
        schema=vol.Schema(
            {
                vol.Optional(
                    CONF_MAX_ACCURACY, default=DEFAULT_MAX_ACCURACY
                ): NumberSelector(
                    NumberSelectorConfig(
                        mode=NumberSelectorMode.BOX,
                        min=0.0,
                        unit_of_measurement="m",
                    )
                ),
                vol.Optional(
                    CONF_SKIP_ACCURACY_FILTER_FOR, default=[]
                ): _FREE_TEXT_LIST,
            }
        )
    ),
    "attributes": SchemaFlowFormStep(
        schema=_attributes_schema,
        suggested_values=_attributes_suggested,
        validate_user_input=_validate_attributes,
    ),
    "devices": SchemaFlowFormStep(
        schema=vol.Schema(
            {
                vol.Optional(
                    CONF_AUTO_ADD_DEVICES, default=DEFAULT_AUTO_ADD_DEVICES
                ): BooleanSelector(),
                vol.Optional(CONF_CONFIRM, default=False): BooleanSelector(),
            }
        ),
        validate_user_input=_save_devices,
    ),
}


class TraccarClientExtendedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def _get_server_info(self, user_input: Mapping[str, Any]) -> ServerModel:
        """Verify the credentials by reading server info.

        Uses Home Assistant's shared session deliberately: this call carries the
        bearer token and mints no session cookie, unlike the websocket path.
        """
        client = ApiClient(
            client_session=async_get_clientsession(self.hass),
            host=user_input[CONF_HOST],
            port=user_input[CONF_PORT],
            ssl=user_input[CONF_SSL],
            verify_ssl=user_input[CONF_VERIFY_SSL],
            token=user_input[CONF_API_TOKEN],
        )
        return await client.get_server()

    async def _async_mint_token(self, user_input: Mapping[str, Any]) -> str:
        """Exchange a Traccar login for an API token.

        Two calls: `POST /api/session` authenticates and sets a JSESSIONID
        cookie, then `POST /api/session/token` mints a token against that
        session. The cookie is why this cannot use Home Assistant's shared
        session -- aiohttp's default jar silently drops cookies from bare IP
        hosts, which is exactly how most people reach Traccar, and the token
        request would then be unauthenticated.

        The second call must send a form body. `requestToken` declares a
        `@FormParam`, so Jersey accepts `application/x-www-form-urlencoded` and
        nothing else, and an empty POST is rejected with `415 Unsupported Media
        Type`. Sending the expiration is what gives the request that body, and
        it is needed on its own account: without it Traccar mints a token that
        lasts `DEFAULT_EXPIRATION_DAYS`, which is 7.

        Only the returned token reaches the config entry. The password is used
        for this exchange and discarded, so an entry created this way is
        indistinguishable from one created by pasting a token.
        """
        scheme = "https" if user_input[CONF_SSL] else "http"
        base = f"{scheme}://{user_input[CONF_HOST]}:{user_input[CONF_PORT]}/api"
        verify_ssl = user_input[CONF_VERIFY_SSL]
        # Unsafe unconditionally, for the reason set out in `__init__`: a jar
        # that refuses cookies from a bare IP host drops the JSESSIONID this
        # exchange depends on, and most people reach Traccar by address.
        session = async_create_clientsession(
            self.hass, cookie_jar=CookieJar(unsafe=True)
        )
        timeout = aiohttp.ClientTimeout(total=10)

        async with session.post(
            f"{base}/session",
            data={
                "email": user_input[CONF_EMAIL],
                "password": user_input[CONF_PASSWORD],
            },
            ssl=verify_ssl,
            timeout=timeout,
        ) as response:
            if response.status in (400, 401, 403):
                raise TraccarAuthenticationException("Traccar rejected the login")
            response.raise_for_status()

        # `data=` rather than a bare POST: `requestToken` takes a `@FormParam`,
        # so Jersey consumes form-encoded only and a request carrying no body
        # has no content type to match -- which Traccar answers with a bare
        # `415 Unsupported Media Type` that says nothing about form encoding.
        expiration = (datetime.now(UTC) + TOKEN_EXPIRY).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with session.post(
            f"{base}/session/token",
            data={"expiration": expiration},
            ssl=verify_ssl,
            timeout=timeout,
        ) as response:
            if response.status in (400, 401, 403):
                raise TraccarAuthenticationException("Traccar refused to mint a token")
            response.raise_for_status()
            return (await response.text()).strip()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the two ways to authenticate.

        Signing in is first because it is the one that needs nothing set up in
        Traccar beforehand; finding where tokens are generated is the first
        thing a new user has to go and look up.
        """
        return self.async_show_menu(
            step_id="user", menu_options=["credentials", "token"]
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with a Traccar account and mint a token from it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                user_input = {
                    **user_input,
                    CONF_PORT: _clean_port(user_input[CONF_PORT]),
                }
            except vol.Invalid:
                errors[CONF_PORT] = "invalid_port"
                return self._credentials_form(user_input, errors)
            self._async_abort_entries_match(
                {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
            )
            data = {
                key: value
                for key, value in user_input.items()
                if key not in (CONF_EMAIL, CONF_PASSWORD)
            }
            try:
                data[CONF_API_TOKEN] = await self._async_mint_token(user_input)
                await self._get_server_info(data)
            except TraccarAuthenticationException:
                errors["base"] = "invalid_auth"
            except (TraccarException, aiohttp.ClientError) as exception:
                LOGGER.error("Unable to connect to Traccar Server: %s", exception)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"{data[CONF_HOST]}:{data[CONF_PORT]}",
                    data=data,
                )

        return self._credentials_form(user_input, errors)

    def _credentials_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Show the sign-in form, keeping whatever was already typed."""
        return self.async_show_form(
            step_id="credentials",
            data_schema=self.add_suggested_values_to_schema(
                STEP_CREDENTIALS_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use an API token generated in Traccar by hand."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                user_input = {
                    **user_input,
                    CONF_PORT: _clean_port(user_input[CONF_PORT]),
                }
            except vol.Invalid:
                errors[CONF_PORT] = "invalid_port"
                return self._token_form(user_input, errors)
            self._async_abort_entries_match(
                {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
            )
            try:
                await self._get_server_info(user_input)
            except TraccarAuthenticationException:
                errors["base"] = "invalid_auth"
            except TraccarException as exception:
                LOGGER.error("Unable to connect to Traccar Server: %s", exception)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}",
                    data=user_input,
                )

        return self._token_form(user_input, errors)

    def _token_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Show the token form, keeping whatever was already typed."""
        return self.async_show_form(
            step_id="token",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change connection settings without recreating the entry.

        Without this, moving the server to a new host means deleting the entry
        and losing every entity id along with its history.
        """
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                user_input = {
                    **user_input,
                    CONF_PORT: _clean_port(user_input[CONF_PORT]),
                }
            except vol.Invalid:
                errors[CONF_PORT] = "invalid_port"

        if user_input is not None and not errors:
            try:
                await self._get_server_info(user_input)
            except TraccarAuthenticationException:
                errors["base"] = "invalid_auth"
            except TraccarException as exception:
                LOGGER.error("Unable to connect to Traccar Server: %s", exception)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates=user_input,
                    title=f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}",
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                # What was just rejected wins over what is stored, so a mistyped
                # port does not also discard the new host typed beside it.
                STEP_USER_DATA_SCHEMA,
                {**reconfigure_entry.data, **(user_input or {})},
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after a token stops being accepted."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh API token."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._get_server_info({**reauth_entry.data, **user_input})
            except TraccarAuthenticationException:
                errors["base"] = "invalid_auth"
            except TraccarException as exception:
                LOGGER.error("Unable to connect to Traccar Server: %s", exception)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                CONF_HOST: reauth_entry.data[CONF_HOST],
                CONF_PORT: reauth_entry.data[CONF_PORT],
            },
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Devices are configured as subentries of their server."""
        return {SUBENTRY_TYPE_DEVICE: TraccarDeviceSubentryFlowHandler}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SchemaOptionsFlowHandlerWithReload:
        """Return the options flow.

        The reloading variant applies option changes without an update
        listener. Home Assistant forbids combining the two, and a listener
        would also block async_update_reload_and_abort in the subentry flow.
        """
        return SchemaOptionsFlowHandlerWithReload(config_entry, OPTIONS_FLOW)


class TraccarDeviceSubentryFlowHandler(ConfigSubentryFlow):
    """Add and configure individual tracked devices.

    Unlike a subentry the user invents from nothing, these already exist on the
    Traccar server, so the add step offers a picker of devices that do not yet
    have a subentry rather than asking anyone to type an identifier.
    """

    def _coordinator(self) -> Any:
        return self._get_entry().runtime_data

    def _not_loaded(self) -> bool:
        """Return True when the entry has no runtime data to read devices from.

        The device picker and the profile ranking both read live server state,
        so opening either while the entry is retrying setup would otherwise
        raise and surface as an unexplained failure.
        """
        return self._get_entry().state is not ConfigEntryState.LOADED

    def _device_overrides_selector(self, unique_id: str | None) -> ObjectSelector:
        """Build the overrides table, scoped to what this one device reports."""
        coordinator = self._coordinator()
        keys: set[str] = set()
        if coordinator is not None and (record := coordinator.record_for(unique_id)):
            keys.update(record["attributes"])
        return _overrides_selector(sorted(keys - DENYLIST), allow_ignore=True)

    def _profile_selector(self, unique_id: str | None) -> SelectSelector:
        """Build the profile dropdown, likeliest matches first.

        Traccar reports the protocol a device speaks and, when someone filled it
        in, a free-text model. Neither is reliable enough to choose a profile
        automatically, but both are good enough to save scrolling.
        """
        coordinator = self._coordinator()
        protocol = model = None
        for record in (coordinator.data or {}).values():
            if record["device"].get("uniqueId") == unique_id:
                protocol = record["position"].get("protocol")
                model = record["device"].get("model")
                break

        # Naming the automatic match in the label makes it obvious why a device
        # already has mappings nobody selected.
        auto = match(coordinator.profiles, model)
        auto_label = (
            f"Automatic ({auto.display_name})" if auto else "Automatic (no match)"
        )
        options = [
            {"value": PROFILE_AUTO, "label": auto_label},
            {"value": PROFILE_NONE, "label": "No profile"},
        ]
        options.extend(
            {"value": profile.name, "label": profile.display_name}
            for profile in suggest(coordinator.profiles, protocol, model)
        )
        return SelectSelector(
            SelectSelectorConfig(
                mode=SelectSelectorMode.DROPDOWN, options=options, sort=False
            )
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a device on the server that is not yet tracked."""
        if self._not_loaded():
            return self.async_abort(reason="entry_not_loaded")
        coordinator = self._coordinator()
        candidates = {
            record["device"]["uniqueId"]: (
                record["device"].get("name") or record["device"]["uniqueId"]
            )
            for record in (coordinator.data or {}).values()
            if coordinator.subentry_id_for(record["device"]["uniqueId"]) is None
        }

        if not candidates:
            return self.async_abort(reason="no_devices")

        errors: dict[str, str] = {}
        rows = (user_input or {}).get(CONF_ATTRIBUTE_OVERRIDES) or []
        if user_input is not None and (error := check_override_rows(rows)):
            errors["base"] = error
        elif user_input is not None:
            unique_id = user_input[CONF_DEVICE_UNIQUE_ID]
            traccar_id = next(
                record["device"]["id"]
                for record in coordinator.data.values()
                if record["device"]["uniqueId"] == unique_id
            )
            name = candidates[unique_id]
            model = coordinator.device_model(unique_id)
            settings = device_settings(name, traccar_id)
            if rows:
                settings[CONF_ATTRIBUTE_OVERRIDES] = rows

            # A sub-entry is a model, not a device, so a device whose model is
            # already covered joins that card rather than starting a second
            # one -- which is the whole point of the grouping.
            entry = self._get_entry()
            if (existing := find_subentry(entry, model)) is not None:
                self.hass.config_entries.async_update_subentry(
                    entry, existing, data=with_device(existing, unique_id, settings)
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="added_to_model")

            return self.async_create_entry(
                title=model_title(model, coordinator.profiles),
                data={
                    CONF_MODEL: (model or "").strip(),
                    CONF_DEVICES: {unique_id: settings},
                    CONF_DEVICE_PROFILE: _clean_profile(
                        user_input.get(CONF_DEVICE_PROFILE)
                    ),
                },
                unique_id=model_key(model),
            )

        return self.async_show_form(
            step_id="user",
            # Rejected input is handed back rather than dropped: a whole
            # overrides table is a lot to retype because one unit was mistyped.
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_DEVICE_UNIQUE_ID): SelectSelector(
                            SelectSelectorConfig(
                                mode=SelectSelectorMode.DROPDOWN,
                                options=[
                                    {"value": unique_id, "label": name}
                                    for unique_id, name in sorted(
                                        candidates.items(), key=lambda item: item[1]
                                    )
                                ],
                            )
                        ),
                        vol.Optional(
                            CONF_DEVICE_PROFILE, default=PROFILE_AUTO
                        ): self._profile_selector(
                            user_input and user_input.get(CONF_DEVICE_UNIQUE_ID)
                        ),
                        vol.Optional(
                            CONF_ATTRIBUTE_OVERRIDES, default=[]
                        ): self._device_overrides_selector(
                            user_input and user_input.get(CONF_DEVICE_UNIQUE_ID)
                        ),
                    }
                ),
                user_input or {},
            ),
            errors=errors,
        )

    #: Set when the device picker is used, and read by the step it opens.
    _editing: str = ""
    _pending: dict[str, Any] = {}  # noqa: RUF012 - replaced wholesale, not mutated

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit the settings that apply to every device of this model.

        A sub-entry covers a model, so this is where the profile and the
        attribute overrides belong: `io800` means the same thing on every
        FTC921. The exceptions live behind the device picker -- an accuracy
        filter, an override for the one van with an extra sensor.
        """
        if self._not_loaded():
            return self.async_abort(reason="entry_not_loaded")
        subentry = self._get_reconfigure_subentry()
        devices = device_entries(subentry)
        sample = next(iter(devices), None)

        errors: dict[str, str] = {}
        rows = (user_input or {}).get(CONF_ATTRIBUTE_OVERRIDES) or []
        if user_input is not None and (error := check_override_rows(rows)):
            errors["base"] = error
        elif user_input is not None:
            updates = {
                CONF_ATTRIBUTE_OVERRIDES: rows,
                CONF_MAX_ACCURACY: user_input.get(CONF_MAX_ACCURACY),
                CONF_DEVICE_PROFILE: _clean_profile(
                    user_input.get(CONF_DEVICE_PROFILE)
                ),
            }
            if chosen := user_input.get(CONF_EDIT_DEVICE):
                # Carried rather than saved: saving here would reload the entry
                # and abort the flow before the device step could run.
                self._editing = chosen
                self._pending = updates
                return await self.async_step_device()

            # Reload, not just update: entity descriptions are built during
            # platform setup, so without this the new profile is stored and
            # never applied.
            return self.async_update_reload_and_abort(
                self._get_entry(), subentry, data_updates=updates
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DEVICE_PROFILE, default=PROFILE_AUTO
                ): self._profile_selector(sample),
                vol.Optional(
                    CONF_ATTRIBUTE_OVERRIDES, default=[]
                ): self._device_overrides_selector(sample),
                vol.Optional(CONF_MAX_ACCURACY): NumberSelector(
                    NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=0.0)
                ),
                vol.Optional(CONF_EDIT_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        mode=SelectSelectorMode.DROPDOWN,
                        options=[
                            {
                                "value": unique_id,
                                "label": settings.get(CONF_DEVICE_NAME) or unique_id,
                            }
                            for unique_id, settings in sorted(
                                devices.items(),
                                key=lambda item: item[1].get(CONF_DEVICE_NAME) or "",
                            )
                        ],
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    **subentry.data,
                    CONF_ATTRIBUTE_OVERRIDES: override_rows(
                        subentry.data.get(CONF_ATTRIBUTE_OVERRIDES)
                    ),
                    # What was just rejected wins over what is stored, or a
                    # mistyped unit costs every other edit on the form.
                    **(user_input or {}),
                    # The picker opens a step rather than storing a value, so
                    # it always redisplays empty.
                    CONF_EDIT_DEVICE: None,
                },
            ),
            # Both names, deliberately. Home Assistant caches translations for
            # the life of a run, so an upgraded install renders the *old*
            # string against the new code until it is restarted -- and a
            # placeholder the cached string still asks for and no longer gets
            # fails the whole dialog with a formatjs MISSING_VALUE error.
            description_placeholders={
                "model": subentry.title,
                "device": subentry.title,
            },
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit one device's own settings, and act on its entities."""
        subentry = self._get_reconfigure_subentry()
        unique_id = self._editing
        if not unique_id:
            # Only reachable by the picker on the step before, but a flow that
            # wrote `with_device(subentry, "", ...)` would add a phantom device
            # to the model that nothing could ever remove.
            return self.async_abort(reason="entry_not_loaded")
        settings = device_entries(subentry).get(unique_id, {})

        errors: dict[str, str] = {}
        rows = (user_input or {}).get(CONF_ATTRIBUTE_OVERRIDES) or []
        if user_input is not None and (error := check_override_rows(rows)):
            errors["base"] = error
        elif user_input is not None:
            entry = self._get_entry()
            if user_input.get(CONF_ENABLE_ALL_ENTITIES):
                enable_subentry_entities(
                    self.hass, entry.entry_id, subentry.subentry_id, unique_id
                )
            elif user_input.get(CONF_RESET_ENTITIES):
                reset_subentry_entities(
                    self.hass,
                    entry.runtime_data,
                    entry.entry_id,
                    subentry.subentry_id,
                    unique_id,
                )

            updated = {**settings, CONF_ATTRIBUTE_OVERRIDES: rows}
            if (accuracy := user_input.get(CONF_MAX_ACCURACY)) is None:
                # Cleared rather than stored as None, so "unset" falls through
                # to the model's value instead of overriding it with nothing.
                updated.pop(CONF_MAX_ACCURACY, None)
            else:
                updated[CONF_MAX_ACCURACY] = accuracy

            return self.async_update_reload_and_abort(
                entry,
                subentry,
                data_updates={
                    **self._pending,
                    CONF_DEVICES: with_device(subentry, unique_id, updated)[
                        CONF_DEVICES
                    ],
                },
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ATTRIBUTE_OVERRIDES, default=[]
                ): self._device_overrides_selector(unique_id),
                vol.Optional(CONF_MAX_ACCURACY): NumberSelector(
                    NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=0.0)
                ),
                vol.Optional(
                    CONF_ENABLE_ALL_ENTITIES, default=False
                ): BooleanSelector(),
                vol.Optional(CONF_RESET_ENTITIES, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="device",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    **settings,
                    CONF_ATTRIBUTE_OVERRIDES: override_rows(
                        settings.get(CONF_ATTRIBUTE_OVERRIDES)
                    ),
                    **(user_input or {}),
                    # Actions, not settings: they never redisplay ticked.
                    CONF_ENABLE_ALL_ENTITIES: False,
                    CONF_RESET_ENTITIES: False,
                },
            ),
            description_placeholders={
                "device": settings.get(CONF_DEVICE_NAME) or unique_id
            },
            errors=errors,
        )


#: The two reasons an entity of ours is switched off that "enable all" should
#: undo. CONFIG_ENTRY, DEVICE and HASS mean something larger is disabled, and
#: clearing them here would neither work nor be what anyone asked for.
_CLEARABLE_DISABLERS: Final = frozenset(
    {er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER}
)


@callback
def enable_subentry_entities(
    hass: HomeAssistant, entry_id: str, subentry_id: str, device_unique_id: str
) -> int:
    """Switch on every disabled entity of one device. Returns how many.

    Scoped to the device, not the sub-entry. A sub-entry is a model now and
    holds the whole fleet of it, so filtering on the sub-entry alone would
    switch on every FTC921 in the place from a form headed with one van's name.

    A one-shot action, not a setting. Home Assistant owns the enabled state
    once an entity exists -- `entity_registry_enabled_default` only decides
    what a *new* entity starts as -- so this writes to the registry directly
    and stores nothing of its own. There is deliberately no list of what was
    enabled: the registry is the single source of truth, and a copy of it here
    would drift the moment somebody used Home Assistant's own entity settings.
    """
    registry = er.async_get(hass)
    # Entity unique ids are `<device uniqueId>_<key>`, the same shape the
    # coordinator's prune matches on.
    prefix = f"{device_unique_id}_"
    enabled = 0
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.config_subentry_id != subentry_id:
            continue
        if not entity.unique_id.startswith(prefix):
            continue
        if entity.disabled_by not in _CLEARABLE_DISABLERS:
            continue
        registry.async_update_entity(entity.entity_id, disabled_by=None)
        enabled += 1

    LOGGER.debug("Enabled %s previously disabled entity(s)", enabled)
    return enabled


def _clean_profile(value: str | None) -> str:
    """Normalise the dropdown value for storage.

    PROFILE_NONE is stored rather than discarded: "leave it on automatic" and
    "definitely do not apply a profile" have to stay distinguishable.
    """
    return value or PROFILE_AUTO
