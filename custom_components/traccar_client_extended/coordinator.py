"""Data update coordinator for Traccar Client Extended."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, TypedDict

import aiohttp
from pytraccar import (
    ApiClient,
    DeviceModel,
    GeofenceModel,
    PositionModel,
    SubscriptionData,
    TraccarAuthenticationException,
    TraccarException,
)

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .attributes import (
    ATTRIBUTE_KEY_PREFIX,
    DENYLIST,
    AttributeOverride,
    is_known,
    parse_overrides,
)
from .const import (
    ALARMS,
    ATTR_CREATED_BY,
    CONF_ATTRIBUTE_OVERRIDES,
    CONF_AUTO_ADD_DEVICES,
    CONF_CREATE_NOTIFICATIONS,
    CONF_CUSTOM_ATTRIBUTES,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_UNIQUE_ID,
    CONF_DISCOVER_UNKNOWN,
    CONF_EVENTS,
    CONF_IGNORED_ATTRIBUTES,
    CONF_KNOWN_DEVICES,
    CONF_MAX_ACCURACY,
    CONF_MAX_DISCOVERED,
    CONF_SKIP_ACCURACY_FILTER_FOR,
    CONF_TRACCAR_DEVICE_ID,
    CREATED_BY_VALUE,
    DEFAULT_AUTO_ADD_DEVICES,
    DEFAULT_DISCOVER_UNKNOWN,
    DEFAULT_MAX_ACCURACY,
    DEFAULT_MAX_DISCOVERED,
    DEFAULT_PORT,
    DOMAIN,
    EVENTS,
    ISSUE_ALARM_NOTIFICATION_EMPTY,
    ISSUE_NO_WEB_NOTIFICATION,
    ISSUE_SERVER_BUFFERING,
    ISSUE_SUBSCRIPTION_FAILED,
    LOGGER,
    NOTIFICATOR_WEB,
    PROFILE_AUTO,
    PROFILE_NONE,
    SIGNAL_DEVICE_UPDATE,
    SIGNAL_DISCOVERY,
    SIGNAL_EVENT,
    SUBENTRY_TYPE_DEVICE,
    SUBSCRIPTION_FAILURES_BEFORE_REPAIR,
)
from .helpers import get_device, get_geofence_ids, get_geofences
from .profiles import DeviceProfile, match

_SUBSCRIPTION_RECONNECT_DELAY = 10
_SUBSCRIPTION_FAILURE_LOG_EVERY_N = 30
# Bounded memory of event ids already published. Traccar can repeat an event
# across a reconnect, and a synthetic test event carries no id and so is never
# suppressed by it.
_MAX_SEEN_EVENTS = 500

# Traccar stamps a position three times and an event once. Reporting each as an
# age at arrival is what makes a delay attributable; see _latency_report.
_POSITION_STAMPS = ("deviceTime", "fixTime", "serverTime")
_EVENT_STAMPS = ("eventTime",)

# -- Fixed-delay detection ----------------------------------------------------
#
# Traccar holds every incoming position for `server.buffering.threshold` ms
# before releasing it, to reorder locations that arrive out of sequence. The key
# defaults to 3000 and lives in traccar.xml, where no API can reach it and
# nothing surfaces it, so every default install pays three seconds on every
# position and every event without being told.
#
# It is detectable from here because a timer and a bottleneck look different: a
# timer holds its value, contended work scatters. Both conditions have to hold
# before saying anything, so an overloaded server is never told to change a
# setting that would not help it.
_LAG_SAMPLE_SIZE = 50
_LAG_MIN_SAMPLES = 20
# 3000 ms of buffering measured ~3.1 s end to end against ~0.5 s of real
# processing, and 500 ms measured ~1.0 s. Two seconds sits clear of both a
# lowered threshold and ordinary jitter.
_LAG_SUSPECT_SECONDS = 2.0
_LAG_MAX_SPREAD_SECONDS = 1.5
# Beyond this the two clocks disagree enough that nothing can be attributed, and
# the Date header only resolves to the second in the first place.
_MAX_CLOCK_OFFSET_SECONDS = 5.0


def _web_notifications(notifications: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Those notifications that would actually reach a websocket client."""
    return [
        item
        for item in notifications
        if NOTIFICATOR_WEB in (item.get("notificators") or "").split(",")
    ]


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already-sorted sequence."""
    if not ordered:
        return 0.0
    index = round(fraction * (len(ordered) - 1))
    return ordered[min(len(ordered) - 1, max(0, index))]


def _diagnose_fixed_delay(
    samples: Sequence[float], clock_offset: float
) -> tuple[float, float] | None:
    """Return (median, spread) if these arrival lags look like a fixed delay.

    `clock_offset` is how far ahead Home Assistant's clock runs, and is
    subtracted before judging: a host two seconds fast inflates every lag by two
    seconds and would otherwise read as a server-side delay.

    None means "do not assert anything" -- too few samples, clocks too far
    apart to attribute, a median low enough not to matter, or a spread wide
    enough that this is contention rather than a timer.
    """
    if len(samples) < _LAG_MIN_SAMPLES:
        return None
    if abs(clock_offset) > _MAX_CLOCK_OFFSET_SECONDS:
        return None
    corrected = sorted(sample - clock_offset for sample in samples)
    median = _percentile(corrected, 0.5)
    spread = _percentile(corrected, 0.9) - _percentile(corrected, 0.1)
    if median < _LAG_SUSPECT_SECONDS or spread > _LAG_MAX_SPREAD_SECONDS:
        return None
    return median, spread


def _latency_report(
    payload: Mapping[str, Any], keys: tuple[str, ...], now: datetime | None
) -> str:
    """Render Traccar's own timestamps as ages at arrival, for latency triage.

    Traccar stamps a position three times: `deviceTime` when the tracker says
    the fix happened, `fixTime` from the GPS, and `serverTime` when Traccar
    accepted it off the wire. Only the gap between `serverTime` and now is time
    spent inside Traccar plus the network to us -- everything older than
    `serverTime` happened before Traccar ever saw the position, and no change on
    either side could affect it. Splitting them is the only way to tell a slow
    server from a slow tracker without reading the server's own log.

    Ages are signed. A negative one means the stamp is in the future relative to
    Home Assistant, which is clock skew between the two hosts rather than
    latency, and would otherwise be read as a suspiciously fast delivery.
    """
    if now is None:
        return "unknown"
    parts = []
    for key in keys:
        if not (stamp := payload.get(key)):
            continue
        if (parsed := dt_util.parse_datetime(stamp)) is None:
            continue
        # Traccar sends ISO8601 with an offset, but a naive stamp from an older
        # server must not be read as Home Assistant local time.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parts.append(f"{key} {(now - parsed).total_seconds():+.1f}s")
    return ", ".join(parts) if parts else "no usable timestamp"


# One shared instance: an ignore carries no other settings, so there is nothing
# to distinguish one from another.
_IGNORED = AttributeOverride(platform="ignore")


class TraccarDeviceData(TypedDict):
    """Everything the integration knows about a single tracked device."""

    device: DeviceModel
    position: PositionModel
    geofences: list[GeofenceModel]
    attributes: dict[str, Any]


type TraccarCoordinatorData = dict[int, TraccarDeviceData]
type TraccarConfigEntry = ConfigEntry[TraccarClientExtendedCoordinator]


class TraccarClientExtendedCoordinator(DataUpdateCoordinator[TraccarCoordinatorData]):
    """Fetch and hold Traccar Server state.

    Updates arrive over the websocket subscription rather than on a timer, so
    ``update_interval`` is None and ``_async_update_data`` only runs for the
    initial refresh and any explicitly requested reload.
    """

    config_entry: TraccarConfigEntry

    #: uniqueId -> record, rebuilt on demand. A class-level default keeps this
    #: safe for instances built without __init__ in tests.
    _record_index: dict[str, TraccarDeviceData] | None = None
    #: device id -> its attribute key set, so an update only has to diff the
    #: devices that actually changed.
    _device_keys: dict[int, frozenset[str]] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: TraccarConfigEntry,
        client: ApiClient,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        # pytraccar covers no notification endpoints, and the event
        # delivery check needs them.
        self._session = session
        self._token: str = config_entry.data[CONF_API_TOKEN]
        self._verify_ssl: bool = config_entry.data.get(CONF_VERIFY_SSL, True)

        options = config_entry.options
        self.max_accuracy: float = options.get(CONF_MAX_ACCURACY, DEFAULT_MAX_ACCURACY)
        self.skip_accuracy_filter_for: list[str] = options.get(
            CONF_SKIP_ACCURACY_FILTER_FOR, []
        )
        self.custom_attributes: list[str] = options.get(CONF_CUSTOM_ATTRIBUTES, [])
        self.events: list[str] = options.get(CONF_EVENTS, [])
        self.discover_unknown: bool = options.get(
            CONF_DISCOVER_UNKNOWN, DEFAULT_DISCOVER_UNKNOWN
        )
        self.max_discovered: int = options.get(
            CONF_MAX_DISCOVERED, DEFAULT_MAX_DISCOVERED
        )
        self.attribute_overrides: dict[str, AttributeOverride] = parse_overrides(
            options.get(CONF_ATTRIBUTE_OVERRIDES)
        )
        self.ignored_attributes: frozenset[str] = frozenset(
            options.get(CONF_IGNORED_ATTRIBUTES) or ()
        )
        self.auto_add_devices: bool = options.get(
            CONF_AUTO_ADD_DEVICES, DEFAULT_AUTO_ADD_DEVICES
        )
        # Absent means an entry created before this option existed, and those
        # must default to False rather than to the form's default: an upgrade
        # that silently writes six objects to the user's Traccar server is
        # exactly the consent problem the checkbox exists to avoid.
        self.create_notifications: bool = config_entry.data.get(
            CONF_CREATE_NOTIFICATIONS, False
        )
        # Populated during setup, before platforms are forwarded.
        self.profiles: dict[str, DeviceProfile] = {}

        scheme = "https" if config_entry.data.get(CONF_SSL) else "http"
        host = config_entry.data[CONF_HOST]
        port = config_entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.server_url = f"{scheme}://{host}:{port}"

        # Rolling window of serverTime-to-arrival lags, and how far ahead this
        # host's clock runs, learned from the Date header on any REST response.
        # Without an offset nothing is asserted: skew and delay are the same
        # number until one of them is measured separately.
        self._lag_samples: deque[float] = deque(maxlen=_LAG_SAMPLE_SIZE)
        self._lag_since_evaluation = 0
        self._clock_offset: float | None = None
        self._buffering_issue_raised = False

        self._geofences: list[GeofenceModel] = []
        self._seen_event_ids: OrderedDict[int, None] = OrderedDict()
        self._consecutive_subscription_failures = 0
        self._should_log_subscription_error = True

        # Listeners run after self.data has been assigned, which is why
        # discovery hangs off one rather than being dispatched from inside
        # _async_update_data. Safe with update_interval=None: _schedule_refresh
        # returns early when there is no interval, so this starts no polling.
        self.async_add_listener(self._notify_discovery)

    # -- Fetching -------------------------------------------------------------

    async def _async_update_data(self) -> TraccarCoordinatorData:
        """Fetch the full device, position and geofence state."""
        try:
            devices, positions, geofences = await asyncio.gather(
                self.client.get_devices(),
                self.client.get_positions(),
                self.client.get_geofences(),
            )
        except TraccarAuthenticationException as ex:
            raise ConfigEntryAuthFailed from ex
        except TraccarException as ex:
            raise UpdateFailed(f"Error while updating device data: {ex}") from ex

        self._geofences = geofences
        previous = self.data or {}
        data: TraccarCoordinatorData = {}

        for position in positions:
            device_id = position["deviceId"]
            if (device := get_device(device_id, devices)) is None:
                LOGGER.debug("No device for position %s", position["id"])
                continue

            if not self._accuracy_ok(device, position):
                if (last := previous.get(device_id)) is not None:
                    # Keep the last good fix rather than dropping the device.
                    LOGGER.debug(
                        "Position %s for device %s rejected by accuracy filter",
                        position["id"],
                        device_id,
                    )
                    data[device_id] = {**last, "device": device}
                    continue
                # Nothing better to show yet. A device with no entities at all
                # is worse than one sitting on an imprecise first fix.
                LOGGER.debug(
                    "Accepting inaccurate first position %s for device %s",
                    position["id"],
                    device_id,
                )

            data[device_id] = self._build(device, position)

        self.sync_subentries(devices)
        return data

    def _build(self, device: DeviceModel, position: PositionModel) -> TraccarDeviceData:
        """Assemble the per-device record."""
        return {
            "device": device,
            "position": position,
            "geofences": get_geofences(
                self._geofences, get_geofence_ids(device, position)
            ),
            "attributes": dict(position.get("attributes") or {}),
        }

    def _accuracy_ok(self, device: DeviceModel, position: PositionModel) -> bool:
        """Return True if the position passes the accuracy filter.

        The filter is off by default. ``skip_accuracy_filter_for`` names
        attributes whose presence exempts a position, matching the semantics of
        core's traccar_server option of the same name. A device subentry may
        set its own threshold, since a phone needs filtering where a hardwired
        tracker does not.
        """
        max_accuracy = self.max_accuracy_for(device.get("uniqueId"))
        if max_accuracy <= 0:
            return True

        for name in self.skip_accuracy_filter_for:
            if name in (position.get("attributes") or {}) or name in (
                device.get("attributes") or {}
            ):
                return True

        return (position.get("accuracy") or 0.0) <= max_accuracy

    # -- Websocket ------------------------------------------------------------

    async def handle_subscription_data(self, data: SubscriptionData) -> None:
        """Apply an incremental update pushed over the websocket."""
        if self._consecutive_subscription_failures:
            LOGGER.info(
                "Traccar subscription restored after %s failed attempt(s)",
                self._consecutive_subscription_failures,
            )
            self._consecutive_subscription_failures = 0
            self._clear_subscription_issue()
        self._should_log_subscription_error = True

        # Sampled once per payload so every latency line below is measured
        # against the same arrival instant and the numbers stay comparable. Both
        # this and the reports it feeds are skipped entirely unless debug is on:
        # this method runs on every position for every device.
        arrived = dt_util.utcnow()
        debug = LOGGER.isEnabledFor(logging.DEBUG)
        if debug:
            LOGGER.debug(
                "Websocket payload: %s device(s), %s position(s), %s event(s)",
                len(data.get("devices") or []),
                len(data.get("positions") or []),
                len(data.get("events") or []),
            )

        if self.data is None:
            LOGGER.debug("Ignoring websocket payload: no data fetched yet")
            return

        updated: set[int] = set()
        unknown_device = False

        for device in data.get("devices") or []:
            device_id = device["id"]
            if device_id not in self.data:
                unknown_device = True
                continue
            self.data[device_id]["device"] = device
            updated.add(device_id)

        for position in data.get("positions") or []:
            device_id = position["deviceId"]
            if (current := self.data.get(device_id)) is None:
                unknown_device = True
                continue
            if not self._accuracy_ok(current["device"], position):
                LOGGER.debug(
                    "Position %s for device %s rejected by accuracy filter",
                    position["id"],
                    device_id,
                )
                continue
            self.data[device_id] = self._build(current["device"], position)
            updated.add(device_id)
            self._sample_server_lag(position, arrived)
            if debug:
                LOGGER.debug(
                    "Position %s for device %s arrival lag: %s",
                    position.get("id"),
                    device_id,
                    _latency_report(position, _POSITION_STAMPS, arrived),
                )

        for event in data.get("events") or []:
            if debug:
                LOGGER.debug(
                    "Event %s (%s) for device %s arrival lag: %s",
                    event.get("id"),
                    event.get("type"),
                    event.get("deviceId"),
                    _latency_report(event, _EVENT_STAMPS, arrived),
                )
            self.publish_event(event)

        if updated:
            self._notify_discovery(updated)
        for device_id in updated:
            async_dispatcher_send(self.hass, f"{SIGNAL_DEVICE_UPDATE}_{device_id}")

        if unknown_device:
            # A device was added on the server since our last full fetch.
            await self.async_request_refresh()

    async def subscribe(self) -> None:
        """Keep a websocket subscription open for the life of the config entry."""
        reconnecting = False
        while True:
            if reconnecting:
                # Traccar's AsyncSocket.onWebSocketOpen replays positions only,
                # never device records, so a status change during the outage
                # would never reach us. Without this a device that came back
                # online stays marked offline, and every live sensor with it.
                await self.async_request_refresh()
            reconnecting = True

            try:
                await self.client.subscribe(self.handle_subscription_data)
            except asyncio.CancelledError:
                raise
            except TraccarAuthenticationException as ex:
                # ConfigEntryAuthFailed only becomes a reauth flow when raised
                # from setup or a coordinator refresh. Raised here it would kill
                # this background task, taking the reconnect loop with it, and
                # the integration would go quiet with no prompt and no retry.
                LOGGER.error(
                    "Traccar rejected the API token, starting reauthentication: %s", ex
                )
                self.config_entry.async_start_reauth(self.hass)
                return
            except TraccarException as ex:
                self._log_subscription_failure(
                    "Error while subscribing to Traccar", ex, traceback=False
                )
            except Exception as ex:  # noqa: BLE001
                # A dead background task is worse than a logged surprise.
                self._log_subscription_failure(
                    "Unexpected error while subscribing to Traccar", ex, traceback=True
                )
            else:
                self._consecutive_subscription_failures = 0
                self._should_log_subscription_error = True

            # pytraccar catches CancelledError inside its own subscribe() and
            # returns normally, so an unload does not propagate here. Without
            # this check the loop kept reconnecting until Home Assistant timed
            # out waiting for the task, and it is also the only point at which
            # an unload is distinguishable from a dropped connection.
            if (task := asyncio.current_task()) is not None and task.cancelling():
                LOGGER.debug("Config entry unloading, stopping Traccar subscription")
                raise asyncio.CancelledError

            LOGGER.debug(
                "Traccar subscription ended, reconnecting in %ss",
                _SUBSCRIPTION_RECONNECT_DELAY,
            )
            await asyncio.sleep(_SUBSCRIPTION_RECONNECT_DELAY)

    def _log_subscription_failure(
        self, prefix: str, ex: Exception, *, traceback: bool
    ) -> None:
        """Log a subscription failure, throttling repeats to avoid log spam."""
        self._consecutive_subscription_failures += 1
        info = ex if traceback else None

        if (
            self._consecutive_subscription_failures
            == SUBSCRIPTION_FAILURES_BEFORE_REPAIR
        ):
            self._raise_subscription_issue(str(ex))

        if self._should_log_subscription_error:
            self._should_log_subscription_error = False
            LOGGER.error("%s: %s", prefix, ex, exc_info=info)
            # The usual cause is the session cookie being dropped: aiohttp
            # refuses cookies from bare IP hosts unless the jar is unsafe, and
            # /api/socket authenticates with a cookie rather than the token.
            LOGGER.debug(
                "Traccar authenticates the websocket with a JSESSIONID cookie "
                "obtained from /api/session. If REST calls succeed but this "
                "keeps failing, the cookie is not being stored"
            )
        elif (
            self._consecutive_subscription_failures % _SUBSCRIPTION_FAILURE_LOG_EVERY_N
            == 0
        ):
            LOGGER.warning(
                "Still unable to reconnect to Traccar after %s attempts (last: %s)",
                self._consecutive_subscription_failures,
                ex,
                exc_info=info,
            )

    # -- Events ---------------------------------------------------------------

    @callback
    def publish_event(self, event: dict[str, Any], source: str = "websocket") -> None:
        """Publish a single Traccar event, once.

        Shared by the websocket and the fire_test_event action, which is why
        deduplication lives here rather than in either caller. A synthetic event
        carries no id and so is never suppressed.

        `source` is logged rather than acted on, so a synthetic test event is
        distinguishable from a real one in the log.
        """
        # Every rejection below is logged. This path was silent, which made a
        # missing event impossible to diagnose without reading the source.
        if not self.data:
            LOGGER.debug("Dropping event %s: no device data yet", event.get("type"))
            return

        device_id = event.get("deviceId")
        if (entry := self.data.get(device_id)) is None:
            LOGGER.debug(
                "Dropping %s event: Traccar device %s is not one we track (we have %s)",
                event.get("type"),
                device_id,
                sorted(self.data),
            )
            return
        if (event_type := EVENTS.get(event.get("type"))) is None:
            LOGGER.debug(
                "Dropping event of unmapped type %r; known types are %s",
                event.get("type"),
                sorted(EVENTS),
            )
            return
        # The websocket sends every event type; the REST call is filtered
        # server-side. Apply the user's selection consistently. Empty means all.
        if self.events and event["type"] not in self.events:
            LOGGER.debug(
                "Dropping %s event: the events option restricts this to %s",
                event.get("type"),
                self.events,
            )
            return
        if self._already_published(event.get("id")):
            LOGGER.debug(
                "Dropping event %s (%s): already published",
                event.get("id"),
                event.get("type"),
            )
            return

        attributes = event.get("attributes") or {}
        # Traccar reports every alarm as one `alarm` event with the specific
        # alarm buried in attributes, so a tow and a hard braking event look
        # identical by type. Republish the specific one so an automation can
        # trigger on it directly.
        alarm = attributes.get("alarm")
        specific = event_type
        if event_type == "alarm" and alarm in ALARMS:
            specific = f"alarm_{ALARMS[alarm]}"

        # NotificatorWeb formats a human-readable digest and attaches it as a
        # `message` attribute, and the handlers add structured detail such as
        # speed and speedLimit. Surface all of it rather than making an
        # automation dig through the raw attributes.
        geofence_id = event.get("geofenceId") or None
        payload = {
            "device_traccar_id": device_id,
            "device_name": entry["device"]["name"],
            "type": event["type"],
            "alarm": alarm,
            "message": attributes.get("message"),
            "server_time": event.get("eventTime"),
            "position_id": event.get("positionId") or None,
            "geofence_id": geofence_id,
            "geofence": self._geofence_name(geofence_id),
            "maintenance_id": event.get("maintenanceId") or None,
            "attributes": attributes,
        }
        async_dispatcher_send(
            self.hass, f"{SIGNAL_EVENT}_{device_id}", specific, payload
        )
        LOGGER.debug(
            "Publishing %s event for device %s as %s (via %s)",
            event["type"],
            device_id,
            specific,
            source,
        )
        self.hass.bus.async_fire(f"{DOMAIN}_{event_type}", payload)
        if specific != event_type:
            # Both are fired: one to catch every alarm, one for this alarm.
            self.hass.bus.async_fire(f"{DOMAIN}_{specific}", payload)

    # -- Fixed-delay detection ------------------------------------------------

    @callback
    def _sample_server_lag(
        self, position: Mapping[str, Any], arrived: datetime
    ) -> None:
        """Record how long this position took to reach us after Traccar had it.

        One timestamp parse per position, deliberately: this runs for every
        device on every update, and the other two stamps are only formatted when
        somebody is reading debug output.
        """
        if not (stamp := position.get("serverTime")):
            return
        if (parsed := dt_util.parse_datetime(stamp)) is None:
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        self._lag_samples.append((arrived - parsed).total_seconds())

        self._lag_since_evaluation += 1
        if self._lag_since_evaluation >= _LAG_MIN_SAMPLES:
            self._lag_since_evaluation = 0
            self._evaluate_server_lag()

    @callback
    def _evaluate_server_lag(self) -> None:
        """Raise or clear the buffering repair from the current sample window."""
        issue_id = f"{ISSUE_SERVER_BUFFERING}_{self.config_entry.entry_id}"

        # No offset means no REST call has returned a Date header yet, and
        # without one a fast clock is indistinguishable from a slow server.
        verdict = (
            None
            if self._clock_offset is None
            else _diagnose_fixed_delay(self._lag_samples, self._clock_offset)
        )

        if verdict is None:
            if self._buffering_issue_raised:
                self._buffering_issue_raised = False
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        median, spread = verdict
        LOGGER.debug(
            "Positions arrive %.1fs after Traccar accepts them (spread %.1fs, "
            "clock offset %.1fs); consistent with server.buffering.threshold",
            median,
            spread,
            self._clock_offset,
        )
        if self._buffering_issue_raised:
            return
        self._buffering_issue_raised = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SERVER_BUFFERING,
            translation_placeholders={
                "name": self.config_entry.title,
                "delay": f"{median:.1f}",
            },
        )

    def latency_summary(self) -> dict[str, Any]:
        """Return the current lag window, for diagnostics.

        Reported whether or not it crosses the threshold for a repair: a report
        about updates feeling slow is answered by these numbers, and a spread
        too wide to raise the repair is itself the answer in that case.
        """
        if not self._lag_samples:
            return {"samples": 0}
        ordered = sorted(self._lag_samples)
        return {
            "samples": len(ordered),
            "clock_offset_seconds": self._clock_offset,
            "server_to_ha_p10": round(_percentile(ordered, 0.1), 2),
            "server_to_ha_median": round(_percentile(ordered, 0.5), 2),
            "server_to_ha_p90": round(_percentile(ordered, 0.9), 2),
            "looks_like_fixed_delay": self._buffering_issue_raised,
        }

    def _geofence_name(self, geofence_id: int | None) -> str | None:
        """Resolve a geofence id to its name, for enter and exit events."""
        if not geofence_id:
            return None
        for geofence in self._geofences:
            if geofence["id"] == geofence_id:
                return geofence["name"]
        return None

    def _already_published(self, event_id: int | None) -> bool:
        """Return True if this event id has already been published."""
        if event_id is None:
            # Without an id there is nothing to match on; publishing a rare
            # duplicate beats dropping a real event.
            return False
        if event_id in self._seen_event_ids:
            return True
        self._seen_event_ids[event_id] = None
        while len(self._seen_event_ids) > _MAX_SEEN_EVENTS:
            self._seen_event_ids.popitem(last=False)
        return False

    @property
    def consecutive_subscription_failures(self) -> int:
        """Number of consecutive websocket failures, for system health."""
        return self._consecutive_subscription_failures

    # -- Subentries -----------------------------------------------------------

    def _subentry_map(self) -> dict[str, ConfigSubentry]:
        """Return the live Traccar uniqueId -> subentry lookup.

        Computed rather than cached: editing a subentry replaces the object in
        ``config_entry.subentries``, so a snapshot taken at startup would keep
        serving the settings the device had before it was configured.
        """
        return {
            subentry.data[CONF_DEVICE_UNIQUE_ID]: subentry
            for subentry in self.config_entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_DEVICE
            and CONF_DEVICE_UNIQUE_ID in subentry.data
        }

    def subentry_for(self, device_unique_id: str | None) -> ConfigSubentry | None:
        """Return the subentry configuring a device, if it has one."""
        if device_unique_id is None:
            return None
        return self._subentry_map().get(device_unique_id)

    def subentry_id_for(self, device_unique_id: str | None) -> str | None:
        """Return the subentry id for a device, if it has one."""
        subentry = self.subentry_for(device_unique_id)
        return subentry.subentry_id if subentry else None

    def overrides_for(
        self, device_unique_id: str | None
    ) -> dict[str, AttributeOverride]:
        """Return the attribute overrides in force for a device.

        Entry-level overrides apply to every device; a subentry's own overrides
        are layered on top so one tracker can reinterpret a key differently.
        """
        # Server-wide layer. The ignore picker belongs here, not on top: it is
        # the least specific statement in the system, and applying it last made
        # it the most. That silently beat both a profile describing the hardware
        # and a device override naming the attribute outright, and made "ignore
        # everywhere except this one tracker" impossible to express.
        merged = dict.fromkeys(self.ignored_attributes, _IGNORED)
        merged.update(self.attribute_overrides)
        # Most specific wins: a profile describes the hardware, and the device's
        # own overrides are the escape hatch on top of it.
        if (profile := self.profile_for(device_unique_id)) is not None:
            merged.update(profile.overrides)
        if (subentry := self.subentry_for(device_unique_id)) is not None:
            merged.update(parse_overrides(subentry.data.get(CONF_ATTRIBUTE_OVERRIDES)))
        return merged

    @callback
    def _prune_ignored_entities(self) -> None:
        """Remove the entities of attributes that have been turned off.

        Only attributes explicitly overridden to `ignore` *and* still being
        reported. Never merely-absent ones: a tracker that stops sending a key
        for one position, or goes offline entirely, must not lose its entities.
        Deleting on absence would be this integration deciding a reading is
        gone, which is the one thing it does not do.

        Without this, turning an attribute off leaves its entity in the
        registry as unavailable for ever, which reads as the setting not having
        worked -- and the ignore picker made that two clicks rather than a line
        of override DSL, so everyone who uses it hits this.
        """
        orphans: set[str] = set()
        for record in (self.data or {}).values():
            unique_prefix = record["device"].get("uniqueId")
            overrides = self.overrides_for(unique_prefix)
            orphans.update(
                # An attribute entity is keyed by its attribute name, so the
                # unique id survives the override that makes `describe` stop
                # returning a description for it.
                f"{unique_prefix}_{ATTRIBUTE_KEY_PREFIX}{key}"
                for key in record["attributes"]
                if (override := overrides.get(key)) is not None
                and override.platform == "ignore"
            )
        if not orphans:
            return

        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if entity.unique_id in orphans:
                LOGGER.info(
                    "Removing %s: its attribute is set to ignore", entity.entity_id
                )
                registry.async_remove(entity.entity_id)

    def seen_attribute_keys(self) -> list[str]:
        """Every attribute key the fleet has reported, for the options picker.

        Built from live data rather than a fixed list, because what a Traccar
        device reports depends entirely on its hardware and firmware -- a phone
        sends a handful, an OBD dongle sends dozens, and an unmapped Teltonika
        sends raw `ioNNN` keys nobody could enumerate in advance. Denylisted
        keys are left out: they never become entities, so offering to turn them
        off would suggest they were on.
        """
        keys: set[str] = set()
        for record in (self.data or {}).values():
            keys.update(record["attributes"])
        return sorted(keys - DENYLIST)

    def record_for(self, device_unique_id: str | None) -> TraccarDeviceData | None:
        """Return the coordinator record for a device, by Traccar uniqueId.

        Indexed rather than scanned: this is consulted per device on every
        discovery pass and profile lookup, and a linear scan made the cost
        quadratic in the number of devices.
        """
        if device_unique_id is None:
            return None
        if self._record_index is None:
            self._record_index = {
                unique_id: record
                for record in (self.data or {}).values()
                if (unique_id := record["device"].get("uniqueId"))
            }
        return self._record_index.get(device_unique_id)

    def device_model(self, device_unique_id: str | None) -> str | None:
        """Return the model Traccar reports for a device."""
        record = self.record_for(device_unique_id)
        return record["device"].get("model") if record else None

    def profile_for(self, device_unique_id: str | None) -> DeviceProfile | None:
        """Return the profile in force for a device.

        An explicit choice on the device always wins, including an explicit
        "none". Otherwise the model is matched against the profile library, so
        a fleet of identical trackers is configured once rather than one device
        at a time.
        """
        choice = PROFILE_AUTO
        if (subentry := self.subentry_for(device_unique_id)) is not None:
            choice = subentry.data.get(CONF_DEVICE_PROFILE) or PROFILE_AUTO

        if choice == PROFILE_NONE:
            return None
        if choice != PROFILE_AUTO:
            if (chosen := self.profiles.get(choice)) is not None:
                return chosen
            LOGGER.warning(
                "Device %s is set to profile '%s', which is not installed",
                device_unique_id,
                choice,
            )
            return None

        return match(self.profiles, self.device_model(device_unique_id))

    def max_accuracy_for(self, device_unique_id: str | None) -> float:
        """Return the accuracy threshold in force for a device."""
        if (subentry := self.subentry_for(device_unique_id)) is not None:
            value = subentry.data.get(CONF_MAX_ACCURACY)
            if value is not None:
                return float(value)
        return self.max_accuracy

    def sync_subentries(self, devices: list[DeviceModel]) -> None:
        """Give every newly seen device a subentry.

        "New" means never seen before, not merely lacking a subentry. Those are
        different: a device the user deleted also lacks one, and recreating it
        would make deletion impossible.

        This doubles as the migration path. An install predating subentries has
        none and no record of any device, so on first run every existing device
        gets a subentry and its entities are reassigned without their unique ids
        changing.
        """
        seen: set[str] = {
            unique_id for device in devices if (unique_id := device.get("uniqueId"))
        }
        known = set(self.config_entry.data.get(CONF_KNOWN_DEVICES) or ())

        if self.auto_add_devices:
            existing = set(self._subentry_map())
            for device in devices:
                unique_id = device.get("uniqueId")
                if not unique_id or unique_id in existing or unique_id in known:
                    continue
                self.hass.config_entries.async_add_subentry(
                    self.config_entry,
                    ConfigSubentry(
                        data=MappingProxyType(
                            {
                                CONF_DEVICE_UNIQUE_ID: unique_id,
                                CONF_TRACCAR_DEVICE_ID: device["id"],
                                CONF_DEVICE_NAME: device.get("name") or unique_id,
                            }
                        ),
                        subentry_type=SUBENTRY_TYPE_DEVICE,
                        title=device.get("name") or unique_id,
                        unique_id=unique_id,
                    ),
                )
                existing.add(unique_id)

        # Recorded even when auto-add is off, so turning it on later does not
        # sweep up devices that were deliberately left out.
        if not seen <= known:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_KNOWN_DEVICES: sorted(known | seen),
                },
            )

    def tracked_device_ids(self, devices: list[DeviceModel]) -> set[int]:
        """Return the Traccar ids of devices this entry should track."""
        return {
            device["id"]
            for device in devices
            if device.get("uniqueId") in self._subentry_map()
        }

    # -- Repairs --------------------------------------------------------------

    def _raise_subscription_issue(self, error: str) -> None:
        """Surface a sustained websocket failure as a repair.

        This failure is otherwise invisible: REST keeps working, entities exist,
        and only live updates stop, so without a repair the user sees a working
        integration that silently never changes.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_SUBSCRIPTION_FAILED}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SUBSCRIPTION_FAILED,
            translation_placeholders={
                "name": self.config_entry.title,
                "error": str(error)[:200],
            },
        )

    def _clear_subscription_issue(self) -> None:
        """Remove the repair once the subscription recovers."""
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_SUBSCRIPTION_FAILED}_{self.config_entry.entry_id}",
        )

    # -- Discovery ------------------------------------------------------------

    def discoverable_attributes(self, device_id: int) -> dict[str, Any]:
        """Return the attributes of a device that should become entities.

        Curated keys always pass. Unrecognised keys are included only when
        discovery is enabled, and are capped so a chatty tracker cannot spawn
        an unbounded number of entities.

        Overrides are read through the full layering rather than the server-wide
        set alone, so a profile or a device override can reinstate an attribute
        the ignore picker turned off. Checking the picker separately here would
        put back the precedence bug it was just removed from.
        """
        if not self.data or (entry := self.data.get(device_id)) is None:
            return {}

        overrides = self.overrides_for(entry["device"].get("uniqueId"))
        result: dict[str, Any] = {}
        unknown = 0
        for key, value in sorted(entry["attributes"].items()):
            if (override := overrides.get(key)) is not None:
                # Naming a key is an explicit request for it, so an override
                # bypasses both the denylist and the discovery cap.
                if override.platform != "ignore":
                    result[key] = value
                continue
            if key in DENYLIST:
                continue
            if not is_known(key):
                if not self.discover_unknown or unknown >= self.max_discovered:
                    continue
                unknown += 1
            result[key] = value
        return result

    @callback
    def _notify_discovery(self, changed: set[int] | None = None) -> None:
        """Tell the platforms to look for new devices or attributes.

        Runs on every published update, so it diffs rather than rebuilding: at
        200 devices, recomputing a whole-fleet signature each time cost more
        than everything else the update did, to answer a question whose answer
        is almost always "nothing new".

        ``changed`` names the devices a websocket update touched. The full-fetch
        path passes nothing and every device is checked, which is what a
        coordinator listener call means.
        """
        # The websocket path replaces records inside the same dict, so nothing
        # else would notice the lookup index going stale.
        self._record_index = None

        data = self.data or {}
        if self._device_keys is None:
            self._device_keys = {}

        if set(data) != set(self._device_keys):
            # A device appeared or went away; rebuild and always notify. This
            # is also the first pass after a reload, which is how an attribute
            # just switched off gets its entity removed: the option save
            # reloads the entry, so no key has changed by the time we run.
            self._device_keys = {
                device_id: frozenset(entry["attributes"])
                for device_id, entry in data.items()
            }
            self._prune_ignored_entities()
            async_dispatcher_send(self.hass, SIGNAL_DISCOVERY)
            return

        dirty = False
        for device_id in changed if changed is not None else data:
            if (entry := data.get(device_id)) is None:
                continue
            keys = frozenset(entry["attributes"])
            if keys != self._device_keys.get(device_id):
                self._device_keys[device_id] = keys
                dirty = True

        if dirty:
            # A device started reporting a key that is already turned off.
            self._prune_ignored_entities()
            async_dispatcher_send(self.hass, SIGNAL_DISCOVERY)

    # -- Notification checks --------------------------------------------------
    #
    # Traccar only pushes an event to a websocket client through NotificatorWeb,
    # which fires when a Notification with the "web" channel matches. Without
    # one, every event entity stays silent and nothing anywhere says why. That
    # cost a long debugging session, so the integration now checks and offers to
    # fix it.

    async def _api(
        self, path: str, method: str = "GET", body: Any | None = None
    ) -> Any:
        """Call a Traccar REST endpoint pytraccar does not cover."""
        async with self._session.request(
            method,
            f"{self.server_url}/api/{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            json=body,
            ssl=self._verify_ssl,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            self._record_clock_offset(response.headers.get("Date"))
            response.raise_for_status()
            if response.status == 204 or not response.content_length:
                return None
            return await response.json()

    @callback
    def wanted_event_types(self) -> list[str]:
        """Return the event types worth having a Traccar notification for.

        Every type by default, so nothing is silently undeliverable and the
        user never has to work out which notification is missing. Narrowed to
        the events option when one is set, because a notification for a type
        this integration would discard on arrival is an object on someone's
        server doing nothing.
        """
        return sorted(self.events) if self.events else sorted(EVENTS)

    @callback
    def _record_clock_offset(self, date_header: str | None) -> None:
        """Learn how far ahead this host's clock runs from an HTTP Date header.

        Every arrival lag is measured against Traccar's own `serverTime`, so a
        Home Assistant clock running fast reads as a slow server and vice versa.
        The header resolves only to the second, which is why nothing is claimed
        below a couple of seconds of delay.
        """
        if not date_header:
            return
        try:
            server_now = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return
        if server_now.tzinfo is None:
            server_now = server_now.replace(tzinfo=UTC)
        self._clock_offset = (dt_util.utcnow() - server_now).total_seconds()

    async def async_notifications(self) -> list[dict[str, Any]]:
        """Return the notifications visible to this token's user."""
        return await self._api("notifications") or []

    async def async_create_web_notifications(self, types: list[str]) -> int:
        """Create an always-on web notification for each event type."""
        created = 0
        for event_type in types:
            body: dict[str, Any] = {
                "type": event_type,
                "always": True,
                "notificators": NOTIFICATOR_WEB,
                "calendarId": 0,
                "commandId": 0,
                # Stamped so ours can be told from the user's own later.
                "attributes": {ATTR_CREATED_BY: CREATED_BY_VALUE},
            }
            if event_type == "alarm":
                # An alarm notification with no alarms listed matches nothing,
                # because Traccar's filter returns false rather than defaulting
                # to all of them.
                body["attributes"]["alarms"] = ",".join(ALARMS)
            await self._api("notifications", method="POST", body=body)
            created += 1
        return created

    async def async_check_event_delivery(self) -> None:
        """Raise a repair if Traccar will never push events to us.

        Fails open: a server that cannot answer, or a version without the
        endpoint, must not produce a warning about configuration.
        """
        try:
            notifications = await self.async_notifications()
        except Exception as err:  # noqa: BLE001 - never nag because of an API error
            LOGGER.debug("Could not check notifications: %s", err)
            return

        web = _web_notifications(notifications)

        # Do the server-side setup rather than asking the user to. Only when
        # there is nothing already: someone who has configured a web
        # notification has chosen their types, and adding to them uninvited
        # would be worse than the problem.
        if not web and self.create_notifications:
            try:
                created = await self.async_create_web_notifications(
                    self.wanted_event_types()
                )
            except Exception as err:  # noqa: BLE001 - falls back to the repair
                LOGGER.warning("Could not create Traccar notifications: %s", err)
            else:
                LOGGER.info(
                    "Created %s Traccar web notification(s) so events arrive "
                    "as they happen",
                    created,
                )
                web = _web_notifications(await self.async_notifications())

        issue_id = f"{ISSUE_NO_WEB_NOTIFICATION}_{self.config_entry.entry_id}"
        if web:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            LOGGER.debug("No web notification configured; events will not arrive")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NO_WEB_NOTIFICATION,
                translation_placeholders={"name": self.config_entry.title},
                data={"entry_id": self.config_entry.entry_id},
            )

        # An alarm notification with no alarms listed silently matches nothing.
        incomplete = [
            item
            for item in web
            if item.get("type") == "alarm"
            and not (item.get("attributes") or {}).get("alarms")
        ]
        issue_id = f"{ISSUE_ALARM_NOTIFICATION_EMPTY}_{self.config_entry.entry_id}"
        if incomplete:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ALARM_NOTIFICATION_EMPTY,
                translation_placeholders={"name": self.config_entry.title},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
