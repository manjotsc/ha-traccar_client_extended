"""Constants for the Traccar Client Extended integration."""

from __future__ import annotations

import re as _re
from logging import getLogger
from typing import Final

DOMAIN: Final = "traccar_client_extended"
LOGGER = getLogger(__package__)

DEFAULT_PORT: Final = 8082

# --- Options -----------------------------------------------------------------

CONF_MAX_ACCURACY: Final = "max_accuracy"
CONF_SKIP_ACCURACY_FILTER_FOR: Final = "skip_accuracy_filter_for"
CONF_EVENTS: Final = "events"
CONF_CUSTOM_ATTRIBUTES: Final = "custom_attributes"
CONF_DISCOVER_UNKNOWN: Final = "discover_unknown_attributes"
CONF_ATTRIBUTE_OVERRIDES: Final = "attribute_overrides"

# Attributes to turn off, picked from what the fleet actually reports rather
# than typed. Turning an entity off was previously only expressible as a line of
# override DSL -- "io800: ignore" -- which meant knowing both the syntax and the
# exact key. The picker is built from live data, so the list is what this server
# sends and nothing else.
CONF_IGNORED_ATTRIBUTES: Final = "ignored_attributes"
CONF_AUTO_ADD_DEVICES: Final = "auto_add_devices"
CONF_MAX_DISCOVERED: Final = "max_discovered_attributes"

# Set at config time. Traccar pushes an event to a websocket client only when a
# Notification with the web channel matches, so without one no event ever
# arrives. Creating them is a handful of API calls we are already authenticated
# for, and doing it at setup is the difference between "connect and done" and a
# repair the user has to find. It stays a checkbox rather than an unconditional write
# because these are objects on a server other people may share.
CONF_CREATE_NOTIFICATIONS: Final = "create_notifications"

DEFAULT_MAX_ACCURACY: Final = 0.0
DEFAULT_DISCOVER_UNKNOWN: Final = True
DEFAULT_AUTO_ADD_DEVICES: Final = True
DEFAULT_CREATE_NOTIFICATIONS: Final = True
DEFAULT_MAX_DISCOVERED: Final = 40


# --- Subentries --------------------------------------------------------------

# One subentry per tracked device, so overrides and the accuracy filter can be
# set per device. A mixed fleet needs this: `io800` means different things on
# different Teltonika models, and a phone needs an accuracy filter where a
# hardwired tracker does not.
SUBENTRY_TYPE_DEVICE: Final = "device"

CONF_DEVICE_UNIQUE_ID: Final = "device_unique_id"
CONF_TRACCAR_DEVICE_ID: Final = "traccar_device_id"
CONF_DEVICE_NAME: Final = "device_name"
CONF_DEVICE_PROFILE: Final = "device_profile"

# Devices auto-add has already offered. Kept so deleting a device sticks:
# without it, auto-add would recreate the subentry on the next refresh and the
# deletion would be impossible. Lives in entry.data rather than options because
# the schema options flow replaces the whole options dict on save.
CONF_KNOWN_DEVICES: Final = "known_devices"
CONF_CONFIRM: Final = "confirm"

# Dropdown sentinels. These must be distinguishable: leaving the choice on
# automatic must not mean the same thing as deliberately turning a profile off.
PROFILE_AUTO: Final = "__auto__"
PROFILE_NONE: Final = "__none__"

# --- Repairs -----------------------------------------------------------------

ISSUE_SUBSCRIPTION_FAILED: Final = "subscription_failed"
ISSUE_NO_WEB_NOTIFICATION: Final = "no_web_notification"
ISSUE_ALARM_NOTIFICATION_EMPTY: Final = "alarm_notification_empty"
# Not fixable from here: `server.buffering.threshold` lives in traccar.xml and
# no API writes it. All the integration can do is measure the delay and name the
# key, which still beats the alternative -- it took three sessions and a read of
# Keys.java to find it the first time.
ISSUE_SERVER_BUFFERING: Final = "server_buffering"

# Traccar's channel name for a websocket delivery, from NotificatorManager.
NOTIFICATOR_WEB: Final = "web"

# Stamped into the attributes of every notification this integration creates, so
# they can be told apart from the user's own. They are deliberately *not* removed
# when the entry is deleted: an uninstall that writes to a server which may be
# unreachable is a worse failure than leaving six objects behind, and someone
# reinstalling would only have them recreated.
ATTR_CREATED_BY: Final = "createdBy"
CREATED_BY_VALUE: Final = "home-assistant"

# --- Actions -----------------------------------------------------------------

SERVICE_FIRE_TEST_EVENT: Final = "fire_test_event"
ATTR_EVENT_TYPE: Final = "event_type"

# Consecutive websocket failures before raising a repair. The reconnect delay is
# 10s, so this is roughly a minute of sustained failure.
SUBSCRIPTION_FAILURES_BEFORE_REPAIR: Final = 6

# --- Dispatcher signals ------------------------------------------------------

SIGNAL_DEVICE_UPDATE: Final = f"{DOMAIN}_device_update"
SIGNAL_DISCOVERY: Final = f"{DOMAIN}_discovery"
SIGNAL_EVENT: Final = f"{DOMAIN}_event"

# --- Device tracker attributes ----------------------------------------------

ATTR_CATEGORY: Final = "category"
ATTR_GEOFENCES: Final = "geofences"
ATTR_TRACCAR_ID: Final = "traccar_id"
ATTR_TRACKER: Final = "tracker"

# --- Events ------------------------------------------------------------------

# Traccar event type -> Home Assistant event type. Mirrors Event.java; a type
# missing from here is silently dropped, which is how `unaccompaniedMotion` and
# seven others went unreported.
#: Traccar's own wording for each event type, for the options picker.
#: These are labels rather than a `translation_key`, because Home Assistant
#: requires translation keys to match [a-z0-9-_]+ and Traccar's type names
#: are camelCase -- and the stored option value has to stay the Traccar name,
#: since it is what the notification API is given.
EVENT_LABELS: Final[dict[str, str]] = {
    "alarm": "Alarm",
    "commandResult": "Command result",
    "deviceFuelDrop": "Fuel drop",
    "deviceFuelIncrease": "Fuel increase",
    "deviceInactive": "Device inactive",
    "deviceMoving": "Device moving",
    "deviceOffline": "Status offline",
    "deviceOnline": "Status online",
    "deviceOverspeed": "Speed limit exceeded",
    "deviceStopped": "Device stopped",
    "deviceUnknown": "Status unknown",
    "driverChanged": "Driver changed",
    "geofenceCrossed": "Geofence crossed",
    "geofenceEnter": "Geofence entered",
    "geofenceExit": "Geofence exited",
    "ignitionOff": "Ignition off",
    "ignitionOn": "Ignition on",
    "maintenance": "Maintenance required",
    "media": "Media",
    "proximityEnter": "Linked device nearby",
    "proximityExit": "Linked device away",
    "queuedCommandSent": "Queued command sent",
    "unaccompaniedMotion": "Moving unaccompanied",
}

EVENTS: Final[dict[str, str]] = {
    "alarm": "alarm",
    "commandResult": "command_result",
    "deviceFuelDrop": "device_fuel_drop",
    "deviceFuelIncrease": "device_fuel_increase",
    "deviceInactive": "device_inactive",
    "deviceMoving": "device_moving",
    "deviceOffline": "device_offline",
    "deviceOnline": "device_online",
    "deviceOverspeed": "device_overspeed",
    "deviceStopped": "device_stopped",
    "deviceUnknown": "device_unknown",
    "driverChanged": "driver_changed",
    "geofenceCrossed": "geofence_crossed",
    "geofenceEnter": "geofence_enter",
    "geofenceExit": "geofence_exit",
    "ignitionOff": "ignition_off",
    "ignitionOn": "ignition_on",
    "maintenance": "maintenance",
    "media": "media",
    "proximityEnter": "proximity_enter",
    "proximityExit": "proximity_exit",
    "queuedCommandSent": "queued_command_sent",
    "unaccompaniedMotion": "unaccompanied_motion",
}

# Alarm values from Position.java. Traccar reports every one of these as a
# single `alarm` event with the specific alarm in attributes, so a tow, an SOS
# and a hard braking event are indistinguishable by type alone. These are
# republished as `alarm_<name>` so an automation can trigger on the one it
# cares about.
_ALARM_VALUES: Final = (
    "general",
    "sos",
    "vibration",
    "movement",
    "lowspeed",
    "overspeed",
    "fallDown",
    "lowPower",
    "lowBattery",
    "fault",
    "powerOff",
    "powerOn",
    "door",
    "lock",
    "unlock",
    "geofence",
    "geofenceEnter",
    "geofenceExit",
    "gpsAntennaCut",
    "accident",
    "tow",
    "idle",
    "highRpm",
    "hardAcceleration",
    "hardBraking",
    "hardCornering",
    "laneChange",
    "fatigueDriving",
    "powerCut",
    "powerRestored",
    "jamming",
    "temperature",
    "parking",
    "bonnet",
    "footBrake",
    "fuelLeak",
    "tampering",
    "removing",
)

ALARMS: Final[dict[str, str]] = {
    value: _re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower() for value in _ALARM_VALUES
}

#: Every event type an event entity can report.
EVENT_TYPES: Final[tuple[str, ...]] = tuple(
    sorted(set(EVENTS.values()) | {f"alarm_{name}" for name in ALARMS.values()})
)
