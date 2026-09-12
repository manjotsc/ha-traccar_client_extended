"""Curated mapping of Traccar position attributes to Home Assistant entities.

Traccar's Position.java defines roughly ninety attribute keys, and which of them
a device populates depends entirely on the hardware: a phone running Traccar
Client sends a handful, an OBD dongle sends dozens. Core's traccar_server only
reaches them through an opt-in text list, and what comes back is an untyped
device_tracker attribute with no unit, no device class, and therefore no history
or long-term statistics.

This module maps the keys we know about onto correctly typed entity
descriptions, and falls back to a generic description for keys we do not
recognise so that nothing is silently dropped.

The units are the interesting part. Traccar is inconsistent about them, and the
comments in Position.java are the only documentation:

* distances and odometers are **metres**
* ``hours`` is **milliseconds**
* ``speed`` and ``speedLimit`` are **knots**, but ``obdSpeed`` is **km/h**
* ``batteryLevel`` is a percentage while ``battery`` and ``power`` are **volts**
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfMass,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)

from .const import LOGGER
from .helpers import humanize_key

ATTRIBUTE_KEY_PREFIX: Final = "attr_"

#: Keys that must never become entities: unbounded blobs, or values that change
#: on every position report and would only bloat the recorder.
DENYLIST: Final[frozenset[str]] = frozenset(
    {"raw", "index", "event", "image", "video", "audio", "dtcs"}
)

_NUMBERED_KEY = re.compile(r"^([a-zA-Z]+?)(\d+)$")

_DIAGNOSTIC: Final[dict[str, Any]] = {
    "entity_category": EntityCategory.DIAGNOSTIC,
    "entity_registry_enabled_default": False,
}


@dataclass(frozen=True, kw_only=True)
class TraccarSensorEntityDescription(SensorEntityDescription):
    """Describes a Traccar Client Extended sensor.

    ``value_fn`` is set for entities derived from top-level position or device
    fields; ``attribute_key`` is set for entities derived from
    ``position["attributes"]``. Exactly one of the two is populated.
    """

    value_fn: Callable[[Any], Any] | None = None
    attribute_key: str | None = None
    extra_attributes_fn: Callable[[Any], dict[str, Any] | None] | None = None
    scale: float = 1.0


@dataclass(frozen=True, kw_only=True)
class TraccarBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Traccar Client Extended binary sensor."""

    value_fn: Callable[[Any], bool | None] | None = None
    attribute_key: str | None = None
    invert: bool = False


def _sensor(key: str, **kwargs: Any) -> TraccarSensorEntityDescription:
    """Build a sensor description for a position attribute."""
    kwargs.setdefault("name", humanize_key(key))
    return TraccarSensorEntityDescription(
        key=f"{ATTRIBUTE_KEY_PREFIX}{key}", attribute_key=key, **kwargs
    )


def _binary(key: str, **kwargs: Any) -> TraccarBinarySensorEntityDescription:
    """Build a binary sensor description for a position attribute."""
    kwargs.setdefault("name", humanize_key(key))
    return TraccarBinarySensorEntityDescription(
        key=f"{ATTRIBUTE_KEY_PREFIX}{key}", attribute_key=key, **kwargs
    )


# Odometers all share the same shape: metres in, kilometres shown, and
# monotonically increasing so the statistics engine treats them as a total.
_ODOMETER: Final[dict[str, Any]] = {
    "device_class": SensorDeviceClass.DISTANCE,
    "native_unit_of_measurement": UnitOfLength.METERS,
    "suggested_unit_of_measurement": UnitOfLength.KILOMETERS,
    "state_class": SensorStateClass.TOTAL_INCREASING,
    "suggested_display_precision": 1,
}

_ODOMETER_KEYS: Final = (
    "totalDistance",
    "odometer",
    "tripOdometer",
    "serviceOdometer",
    "obdOdometer",
)

_SENSORS: Final[tuple[TraccarSensorEntityDescription, ...]] = (
    *(_sensor(key, **_ODOMETER) for key in _ODOMETER_KEYS),
    # Distance covered since the previous position, not an odometer.
    _sensor(
        "distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # Engine hours. Traccar stores this in milliseconds.
    _sensor(
        "hours",
        name="Engine hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    # --- Power -------------------------------------------------------------
    _sensor(
        "batteryLevel",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "battery",
        name="Battery voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    _sensor(
        "power",
        name="External power",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    # --- Speed -------------------------------------------------------------
    _sensor(
        "speedLimit",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "obdSpeed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    # --- Fuel --------------------------------------------------------------
    _sensor(
        "fuel",
        name="Fuel remaining",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    _sensor(
        "fuelUsed",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    _sensor(
        "fuelLevel",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "fuelConsumption",
        native_unit_of_measurement="L/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # --- Engine ------------------------------------------------------------
    _sensor(
        "rpm",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "throttle",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "engineLoad",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    *(
        _sensor(
            key,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        )
        for key in ("engineTemp", "coolantTemp", "deviceTemp")
    ),
    _sensor(
        "humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "axleWeight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor(
        "acceleration",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    # --- Wearables ---------------------------------------------------------
    _sensor(
        "heartRate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    _sensor("steps", state_class=SensorStateClass.TOTAL_INCREASING),
    # --- Text --------------------------------------------------------------
    # `alarm` is deliberately a plain string rather than an enum: Traccar
    # protocol decoders are free to emit vendor-specific alarm names, and an
    # enum sensor logs an error for every state outside its declared options.
    _sensor("alarm"),
    _sensor("driverUniqueId", name="Driver"),
    _sensor("status", **_DIAGNOSTIC),
    _sensor("result", name="Command result", **_DIAGNOSTIC),
    *(
        _sensor(key, **_DIAGNOSTIC)
        for key in (
            "vin",
            "versionFw",
            "versionHw",
            "iccid",
            "operator",
            "ip",
            "phone",
            "card",
        )
    ),
    # --- GNSS and signal quality, noisy by nature --------------------------
    *(
        _sensor(key, state_class=SensorStateClass.MEASUREMENT, **_DIAGNOSTIC)
        for key in ("sat", "satVisible", "rssi", "gSensor")
    ),
    *(
        _sensor(
            key,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            **_DIAGNOSTIC,
        )
        for key in ("hdop", "vdop", "pdop")
    ),
)

_BINARY_SENSORS: Final[tuple[TraccarBinarySensorEntityDescription, ...]] = (
    _binary("ignition", device_class=BinarySensorDeviceClass.RUNNING),
    _binary("motion", device_class=BinarySensorDeviceClass.MOTION),
    _binary(
        "charge",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    _binary("door", device_class=BinarySensorDeviceClass.DOOR),
    # Home Assistant's LOCK device class is inverted relative to Traccar's: HA
    # treats "on" as unlocked, while Traccar's `lock: true` means locked.
    _binary("lock", device_class=BinarySensorDeviceClass.LOCK, invert=True),
    _binary("blocked"),
    _binary("armed"),
    _binary("roaming", **_DIAGNOSTIC),
    _binary("approximate", **_DIAGNOSTIC),
    _binary("gps", name="GPS fix", **_DIAGNOSTIC),
)

KNOWN_SENSORS: Final[dict[str, TraccarSensorEntityDescription]] = {
    description.attribute_key: description
    for description in _SENSORS
    if description.attribute_key is not None
}

KNOWN_BINARY_SENSORS: Final[dict[str, TraccarBinarySensorEntityDescription]] = {
    description.attribute_key: description
    for description in _BINARY_SENSORS
    if description.attribute_key is not None
}

# Numbered families, so temp1/temp2/temp3 are typed without enumerating them.
_PREFIX_SENSORS: Final[dict[str, dict[str, Any]]] = {
    "temp": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT,
        "suggested_display_precision": 1,
    },
    "adc": {
        "device_class": SensorDeviceClass.VOLTAGE,
        "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
        "state_class": SensorStateClass.MEASUREMENT,
        "suggested_display_precision": 2,
        **_DIAGNOSTIC,
    },
    "count": {"state_class": SensorStateClass.TOTAL_INCREASING, **_DIAGNOSTIC},
}

_PREFIX_BINARY_SENSORS: Final[dict[str, dict[str, Any]]] = {
    "in": dict(_DIAGNOSTIC),
    "out": dict(_DIAGNOSTIC),
}


def describe(
    key: str,
    value: Any,
    override: AttributeOverride | None = None,
) -> TraccarSensorEntityDescription | TraccarBinarySensorEntityDescription | None:
    """Return the entity description for a Traccar attribute.

    A user override wins outright, then curated keys, then numbered families
    such as ``temp1``, then a generic description inferred from the value's
    type. Returns ``None`` when the key is denylisted, overridden to ``ignore``,
    or the value cannot be represented as a state.
    """
    if override is not None:
        return _from_override(key, value, override)

    if key in DENYLIST:
        return None

    if (binary := KNOWN_BINARY_SENSORS.get(key)) is not None:
        return binary
    if (sensor := KNOWN_SENSORS.get(key)) is not None:
        return sensor

    if match := _NUMBERED_KEY.match(key):
        prefix = match.group(1).lower()
        if prefix in _PREFIX_BINARY_SENSORS:
            return _binary(key, **_PREFIX_BINARY_SENSORS[prefix])
        if prefix in _PREFIX_SENSORS:
            return _sensor(key, **_PREFIX_SENSORS[prefix])
        if prefix == "io":
            # Traccar's generic IO ports carry either a flag or a reading.
            if isinstance(value, bool):
                return _binary(key, **_DIAGNOSTIC)
            return _sensor(key, state_class=SensorStateClass.MEASUREMENT, **_DIAGNOSTIC)

    return _generic(key, value)


def _generic(
    key: str, value: Any
) -> TraccarSensorEntityDescription | TraccarBinarySensorEntityDescription | None:
    """Infer a description for an attribute we have no mapping for.

    Deliberately enabled rather than hidden: the point of discovery is that an
    unrecognised attribute still shows up instead of being lost.
    """
    # bool must be tested before int, since bool subclasses int.
    if isinstance(value, bool):
        return _binary(key)
    if isinstance(value, (int, float)):
        return _sensor(key, state_class=SensorStateClass.MEASUREMENT)
    if isinstance(value, str):
        return _sensor(key)
    # Nested structures have no sensible single-state representation.
    return None


def is_known(key: str) -> bool:
    """Return True if the key has a curated mapping.

    Numbered families count as known, so a tracker reporting temp1..temp8 does
    not burn eight slots of the auto-discovery budget.
    """
    if key in KNOWN_SENSORS or key in KNOWN_BINARY_SENSORS:
        return True
    if match := _NUMBERED_KEY.match(key):
        prefix = match.group(1).lower()
        return (
            prefix in _PREFIX_SENSORS
            or prefix in _PREFIX_BINARY_SENSORS
            or prefix == "io"
        )
    return False


# -- User overrides -----------------------------------------------------------
#
# Traccar hands protocol decoders a free hand with unregistered IO parameters.
# Teltonika, for example, stores anything it has no mapping for as `io<id>`
# holding a raw integer -- so an external voltage arrives as 12500 millivolts
# with no hint that it is a voltage at all. No amount of curation can cover
# every vendor's IO numbering, so the mapping has to be user-overridable.


@dataclass(frozen=True)
class AttributeOverride:
    """A user-supplied mapping for one Traccar attribute."""

    platform: str | None = None  # "sensor", "binary_sensor" or "ignore"
    device_class: str | None = None
    unit: str | None = None
    scale: float = 1.0
    precision: int | None = None
    name: str | None = None


_SCALE_TOKEN = re.compile(r"^x(?P<value>-?\d+(?:\.\d+)?(?:e-?\d+)?)$", re.IGNORECASE)
_PRECISION_TOKEN = re.compile(r"^p(?P<value>\d+)$", re.IGNORECASE)

_PLATFORM_TOKENS = {
    "sensor": "sensor",
    "binary": "binary_sensor",
    "binary_sensor": "binary_sensor",
    "ignore": "ignore",
    "skip": "ignore",
}


# Column names of one row in the overrides table. Shared by the form and the
# parser, so renaming one cannot silently desynchronise them.
OVERRIDE_ATTRIBUTE: Final = "attribute"
OVERRIDE_TREAT_AS: Final = "treat_as"
OVERRIDE_DEVICE_CLASS: Final = "device_class"
OVERRIDE_UNIT: Final = "unit"
OVERRIDE_SCALE: Final = "scale"
OVERRIDE_PRECISION: Final = "precision"
OVERRIDE_NAME: Final = "name"


DEVICE_CLASS_UNIT_INVALID: Final = "invalid_device_class_unit"
DEVICE_CLASS_UNIT_MISSING: Final = "device_class_needs_unit"


def _unit_set(device_class: str | None) -> set[Any] | None:
    """Units Home Assistant constrains a device class to, or None if it does not."""
    if not device_class:
        return None
    try:
        sensor_class = SensorDeviceClass(device_class)
    except ValueError:
        # May be a binary sensor device class, which carries no units at all.
        return None
    return DEVICE_CLASS_UNITS.get(sensor_class)


def allowed_units(device_class: str | None) -> list[str]:
    """Readable list of units a device class accepts, for an error message."""
    units = _unit_set(device_class)
    return sorted(str(unit) for unit in units if unit is not None) if units else []


def check_device_class_unit(device_class: str | None, unit: str | None) -> str | None:
    """Return an error key if Home Assistant would not pair these.

    Home Assistant accepts a mismatch without complaint and then silently drops
    the entity from long-term statistics, so the reading looks right on a card
    while its history stays empty.

    Both the profile loader and the options form check through here. A second
    copy of this rule would drift, and a drifted copy of a rule whose whole
    point is catching a silent failure fails silently itself.
    """
    if (units := _unit_set(device_class)) is None:
        return None
    if not (unit := (unit or "").strip()):
        return None if None in units else DEVICE_CLASS_UNIT_MISSING
    return (
        None
        if unit in {str(item) for item in units if item is not None}
        else (DEVICE_CLASS_UNIT_INVALID)
    )


def override_rows(raw: Any) -> list[dict[str, Any]]:
    """Normalise stored overrides into table rows.

    Rows are the storage shape. Text is what this option held before the table
    existed, and is converted on read so an existing install keeps working and
    is rewritten the first time the form is saved.

    That text branch is a reader for old data, not a second way to write it, and
    can be deleted once no entry predates the table.
    """
    if not raw:
        return []
    if isinstance(raw, list) and any(isinstance(row, dict) for row in raw):
        # `any`, not `all`: a list carrying even one row is the table shape, and
        # feeding it to the line parser calls .strip() on a dict and takes the
        # whole entry down during setup.
        return [
            row for row in raw if isinstance(row, dict) and row.get(OVERRIDE_ATTRIBUTE)
        ]
    return [
        _override_to_row(key, override)
        for key, override in _parse_override_text(raw).items()
    ]


def parse_overrides(raw: Any) -> dict[str, AttributeOverride]:
    """Return the attribute overrides in a stored option, keyed by attribute."""
    return {
        row[OVERRIDE_ATTRIBUTE]: _row_to_override(row) for row in override_rows(raw)
    }


def _row_to_override(row: dict[str, Any]) -> AttributeOverride:
    """Build the runtime override one table row describes.

    Empty cells mean "not specified" rather than "set to nothing", so each falls
    back to the value the un-overridden attribute would have had.
    """
    scale = row.get(OVERRIDE_SCALE)
    precision = row.get(OVERRIDE_PRECISION)
    return AttributeOverride(
        platform=row.get(OVERRIDE_TREAT_AS) or None,
        device_class=row.get(OVERRIDE_DEVICE_CLASS) or None,
        unit=row.get(OVERRIDE_UNIT) or None,
        scale=1.0 if scale in (None, "") else float(scale),
        precision=None if precision in (None, "") else int(precision),
        name=row.get(OVERRIDE_NAME) or None,
    )


def _override_to_row(key: str, override: AttributeOverride) -> dict[str, Any]:
    """Render one override as a table row, omitting everything unset.

    Empty cells are left out rather than written as nulls: the table shows one
    input per column, and a row of explicit empties reads as configuration
    somebody chose.
    """
    row: dict[str, Any] = {OVERRIDE_ATTRIBUTE: key}
    if override.platform:
        row[OVERRIDE_TREAT_AS] = override.platform
    if override.device_class:
        row[OVERRIDE_DEVICE_CLASS] = override.device_class
    if override.unit:
        row[OVERRIDE_UNIT] = override.unit
    if override.scale != 1.0:
        row[OVERRIDE_SCALE] = override.scale
    if override.precision is not None:
        row[OVERRIDE_PRECISION] = override.precision
    if override.name:
        row[OVERRIDE_NAME] = override.name
    return row


def _parse_override_text(raw: Any) -> dict[str, AttributeOverride]:
    """Parse the pre-table text format: one override per line.

        io800: voltage, V, x0.001
        io801: temperature, °C, x0.1, p1
        io234: binary
        io999: ignore

    Unparsable lines are logged and skipped rather than breaking setup, since
    this was free text typed into a form.
    """
    lines = raw.splitlines() if isinstance(raw, str) else list(raw)

    overrides: dict[str, AttributeOverride] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            LOGGER.warning("Ignoring attribute override without ':' -> %s", line)
            continue

        key, _, spec = line.partition(":")
        key = key.strip()
        if not key:
            LOGGER.warning("Ignoring attribute override with no key -> %s", line)
            continue

        if (parsed := _parse_override_spec(key, spec)) is not None:
            overrides[key] = parsed

    return overrides


def _parse_override_spec(key: str, spec: str) -> AttributeOverride | None:
    """Parse the right-hand side of a single override line."""
    platform: str | None = None
    device_class: str | None = None
    unit: str | None = None
    scale = 1.0
    precision: int | None = None
    name: str | None = None

    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue

        lowered = token.lower()
        if lowered in _PLATFORM_TOKENS:
            platform = _PLATFORM_TOKENS[lowered]
        elif match := _SCALE_TOKEN.match(token):
            scale = float(match.group("value"))
        elif match := _PRECISION_TOKEN.match(token):
            precision = int(match.group("value"))
        elif lowered.startswith("name="):
            name = token[5:].strip() or None
        elif (resolved := _resolve_device_class(lowered)) is not None:
            device_class, implied = resolved
            if platform is None:
                platform = implied
        else:
            # Anything left over is the unit of measurement.
            unit = token

    if platform is None and device_class is None and unit is None:
        LOGGER.warning("Ignoring empty attribute override for %s", key)
        return None

    if platform is None:
        platform = "sensor"

    return AttributeOverride(
        platform=platform,
        device_class=device_class,
        unit=unit,
        scale=scale,
        precision=precision,
        name=name,
    )


def _resolve_device_class(value: str) -> tuple[str, str] | None:
    """Return (device class, implied platform) for a device class name."""
    # Several names exist on both platforms ("battery", "power"). Prefer the
    # sensor reading; an explicit "binary" token in the same line overrides it.
    try:
        return SensorDeviceClass(value).value, "sensor"
    except ValueError:
        pass
    try:
        return BinarySensorDeviceClass(value).value, "binary_sensor"
    except ValueError:
        return None


def _from_override(
    key: str, value: Any, override: AttributeOverride
) -> TraccarSensorEntityDescription | TraccarBinarySensorEntityDescription | None:
    """Build an entity description from a user override."""
    if override.platform == "ignore":
        return None

    name = override.name or humanize_key(key)

    if override.platform == "binary_sensor":
        device_class = None
        if override.device_class is not None:
            try:
                device_class = BinarySensorDeviceClass(override.device_class)
            except ValueError:
                LOGGER.warning(
                    "%s is not a binary sensor device class, ignoring it for %s",
                    override.device_class,
                    key,
                )
        return _binary(key, name=name, device_class=device_class)

    device_class = None
    if override.device_class is not None:
        try:
            device_class = SensorDeviceClass(override.device_class)
        except ValueError:
            LOGGER.warning(
                "%s is not a sensor device class, ignoring it for %s",
                override.device_class,
                key,
            )

    kwargs: dict[str, Any] = {
        "name": name,
        "device_class": device_class,
        "native_unit_of_measurement": override.unit,
        "scale": override.scale,
    }
    if override.precision is not None:
        kwargs["suggested_display_precision"] = override.precision
    # A unit or a numeric device class implies something worth graphing, so opt
    # it into long-term statistics rather than leaving it a bare string state.
    if override.unit is not None or device_class is not None:
        kwargs["state_class"] = SensorStateClass.MEASUREMENT

    return _sensor(key, **kwargs)
