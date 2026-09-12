"""Helper functions for the Traccar Client Extended integration."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pytraccar import DeviceModel, GeofenceModel, PositionModel

from homeassistant.util import dt as dt_util

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TRAILING_DIGITS = re.compile(r"^([a-zA-Z]+?)(\d+)$")

# Acronyms that should stay upper case when a key is humanised.
_ACRONYMS = {
    "adc": "ADC",
    "dtc": "DTC",
    "gps": "GPS",
    "hdop": "HDOP",
    "id": "ID",
    "io": "IO",
    "ip": "IP",
    "obd": "OBD",
    "pdop": "PDOP",
    "rpm": "RPM",
    "rssi": "RSSI",
    "sos": "SOS",
    "vdop": "VDOP",
    "vin": "VIN",
}


def get_device(device_id: int, devices: list[DeviceModel]) -> DeviceModel | None:
    """Return the device with the given id, if present."""
    return next((device for device in devices if device["id"] == device_id), None)


def get_geofence_ids(device: DeviceModel, position: PositionModel) -> list[int]:
    """Return the geofence ids a device is currently inside.

    Traccar moved this field from the device to the position in 5.8, so check
    both. See traccar/traccar@30bafae.
    """
    if position.get("geofenceIds"):
        return position["geofenceIds"] or []
    # Older Traccar releases put it on the device instead.
    # Not part of DeviceModel, but present on servers older than 5.8.
    if device_ids := device.get("geofenceIds"):  # type: ignore[typeddict-item]
        return list(device_ids)
    return []


def get_geofences(
    geofences: list[GeofenceModel], target_ids: list[int]
) -> list[GeofenceModel]:
    """Return every geofence the device is inside.

    Core's traccar_server keeps only the first match and discards the rest, so a
    device sitting inside overlapping geofences reports just one of them.
    """
    return [geofence for geofence in geofences if geofence["id"] in target_ids]


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a Traccar ISO timestamp into an aware datetime.

    Traccar sends ``0001-01-01T00:00:00.000+00:00`` for "never", which is not a
    useful state, so treat anything before 1971 as missing.
    """
    if not value or not isinstance(value, str):
        return None
    if (parsed := dt_util.parse_datetime(value)) is None:
        return None
    if parsed.year < 1971:
        return None
    return dt_util.as_utc(parsed)


def humanize_key(key: str) -> str:
    """Turn a Traccar attribute key into a readable entity name.

    ``totalDistance`` -> ``Total distance``, ``temp1`` -> ``Temp 1``,
    ``io12`` -> ``IO 12``, ``rssi`` -> ``RSSI``.
    """
    if match := _TRAILING_DIGITS.match(key):
        stem, number = match.groups()
        return f"{humanize_key(stem)} {number}"

    words = _CAMEL_BOUNDARY.sub(" ", key).replace("_", " ").split()
    if not words:
        return key

    rendered = [
        _ACRONYMS[word.lower()] if word.lower() in _ACRONYMS else word.lower()
        for word in words
    ]
    first = rendered[0]
    if first not in _ACRONYMS.values():
        first = first.capitalize()
    return " ".join([first, *rendered[1:]])
