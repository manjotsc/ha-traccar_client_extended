"""Device profiles: shareable attribute mappings for known hardware.

Traccar's protocol decoders store anything they have no mapping for under a
generic key holding a raw value -- Teltonika's unregistered AVL parameters
become ``io<id>`` with an unscaled integer, for example. Working out that
``io800`` is an external voltage in millivolts is a puzzle every owner of that
model would otherwise have to solve alone.

A profile is that answer, written once and shared. Files live in
``device_profiles/`` beside this module, so contributing one is adding a JSON
file rather than writing code.

Files whose name starts with an underscore are documentation rather than
profiles and are not loaded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .attributes import (
    DECODERS,
    AttributeField,
    AttributeOverride,
    allowed_units,
    check_device_class_unit,
)
from .const import DOMAIN, LOGGER

PROFILES_DIRECTORY = "device_profiles"

#: A field name becomes part of an entity's unique id, so it has to be a slug.
_FIELD_KEY = re.compile(r"^[a-z0-9_]+$")

_STATE_CLASSES = {item.value for item in SensorStateClass}

_DIAGNOSTIC_CATEGORY = EntityCategory.DIAGNOSTIC.value

_DATA_PROFILES = f"{DOMAIN}_profiles"


@dataclass(frozen=True)
class DeviceProfile:
    """A shareable set of attribute mappings for one device model."""

    name: str
    display_name: str
    vendor: str | None = None
    description: str | None = None
    protocols: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    overrides: dict[str, AttributeOverride] = field(default_factory=dict)


class ProfileError(ValueError):
    """Raised when a profile file is not usable."""


def validate_units(device_class: str | None, unit: str | None) -> None:
    """Raise if a profile pairs a device class with a unit that would be ignored.

    The rule itself lives in `attributes.check_device_class_unit`, shared with
    the options form. Checking it here means a bad contribution fails CI rather
    than reaching users as a sensor that looks fine and never records history.
    """
    if check_device_class_unit(device_class, unit) is not None:
        raise ProfileError(
            f"device class '{device_class}' does not accept unit {unit!r}; "
            f"valid units are {allowed_units(device_class)}"
        )


def _parse_attribute(key: str, raw: Any) -> AttributeOverride:
    """Build an override from one entry of a profile's attributes map."""
    if not isinstance(raw, dict):
        raise ProfileError(f"attribute '{key}' must be an object")

    platform = raw.get("platform")
    if platform is not None and platform not in ("sensor", "binary_sensor", "ignore"):
        raise ProfileError(
            f"attribute '{key}' has unknown platform {platform!r}; "
            "expected sensor, binary_sensor or ignore"
        )

    device_class = raw.get("device_class")
    unit = raw.get("unit")
    if platform in (None, "sensor"):
        validate_units(device_class, unit)

    scale = raw.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise ProfileError(f"attribute '{key}' has a non-numeric scale")

    precision = raw.get("precision")
    if precision is not None and not isinstance(precision, int):
        raise ProfileError(f"attribute '{key}' has a non-integer precision")

    where = f"attribute '{key}'"
    enabled_by_default = _parse_enabled(where, raw)
    entity_category = _parse_entity_category(where, raw)

    decode = raw.get("decode")
    if decode is not None:
        if decode not in DECODERS:
            raise ProfileError(
                f"{where} has unknown decode {decode!r}; "
                f"expected one of {sorted(DECODERS)}"
            )
        if platform not in (None, "sensor"):
            raise ProfileError(f"{where} has decode but is not a sensor")
        for name in ("device_class", "unit", "scale", "offset", "values", "fields"):
            if raw.get(name) is not None:
                raise ProfileError(
                    f"{where} has decode as well as {name!r}; a decoded value "
                    "is text and none of those apply"
                )

    state_class = raw.get("state_class")
    if state_class is not None and state_class not in _STATE_CLASSES:
        raise ProfileError(
            f"attribute '{key}' has unknown state class {state_class!r}; "
            f"expected one of {sorted(_STATE_CLASSES)}"
        )

    offset = raw.get("offset", 0.0)
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        raise ProfileError(f"attribute '{key}' has a non-numeric offset")

    fields = _parse_fields(key, raw.get("fields"))

    values = _parse_values(where, raw.get("values"))
    if values is not None:
        if platform not in (None, "sensor"):
            raise ProfileError(
                f"attribute '{key}' has values but is not a sensor; labels make "
                "the state text, which a binary sensor cannot hold"
            )
        if device_class or unit or float(scale) != 1.0 or float(offset) or state_class:
            raise ProfileError(
                f"attribute '{key}' has values as well as a device class, unit, "
                "scale, offset or state class; a labelled state is text and "
                "none of those apply"
            )

    if platform is None:
        platform = "sensor"

    return AttributeOverride(
        platform=platform,
        device_class=device_class,
        unit=unit,
        scale=float(scale),
        offset=float(offset),
        precision=precision,
        name=raw.get("name"),
        values=values,
        fields=fields,
        state_class=state_class,
        entity_category=entity_category,
        enabled_by_default=enabled_by_default,
        decode=decode,
    )


def _parse_bits(where: str, spec: dict[str, Any]) -> tuple[int, int]:
    """Return where a packed field sits, as a bit offset and a width.

    `byte: 2` is shorthand for `bits: [16, 8]` and is kept because whole-byte
    fields are the common case and vendor tables number them that way.
    """
    byte, bits = spec.get("byte"), spec.get("bits")
    if (byte is None) == (bits is None):
        raise ProfileError(f"{where} needs exactly one of 'byte' or 'bits'")

    if byte is not None:
        if not isinstance(byte, int) or isinstance(byte, bool) or not 0 <= byte <= 7:
            raise ProfileError(f"{where} needs a 'byte' index from 0 to 7")
        return byte * 8, 8

    if (
        not isinstance(bits, list)
        or len(bits) != 2
        or any(not isinstance(n, int) or isinstance(n, bool) for n in bits)
    ):
        raise ProfileError(f"{where} needs 'bits' as [offset, width]")
    offset, width = bits
    if offset < 0 or width < 1 or offset + width > 64:
        raise ProfileError(
            f"{where} has bits [{offset}, {width}]; the offset counts from the "
            "least significant bit and the run must fit inside 64 bits"
        )
    return offset, width


def _parse_enabled(where: str, raw: dict[str, Any]) -> bool:
    """Validate `enabled_by_default`, which defaults to on."""
    enabled = raw.get("enabled_by_default", True)
    if not isinstance(enabled, bool):
        raise ProfileError(f"{where} has a non-boolean enabled_by_default")
    return enabled


def _parse_entity_category(where: str, raw: dict[str, Any]) -> str | None:
    """Validate `entity_category`, which may only ever be `diagnostic`.

    `config` is the only other member, and a sensor carrying it raises
    HomeAssistantError the moment it is added, so the entity simply never
    appears. Nothing a profile describes is a configuration control.
    """
    category = raw.get("entity_category")
    if category is not None and category != _DIAGNOSTIC_CATEGORY:
        raise ProfileError(
            f"{where} has entity category {category!r}; "
            f"only {_DIAGNOSTIC_CATEGORY!r} is accepted"
        )
    return category


def _parse_fields(key: str, raw: Any) -> dict[str, AttributeField] | None:
    """Validate the values packed inside one attribute.

    A field says where it sits with `byte: n` or `bits: [offset, width]`, both
    counting from the least significant -- see `_parse_bits`, where the byte
    form is resolved as shorthand for the other. It started byte-aligned, on
    the grounds that nothing shipping needed anything finer, which was true of
    our two devices and wrong about the thing `device_profiles/` exists for: a
    contributor whose tracker packs a flag into one bit would have had to wait
    for someone to write Python.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ProfileError(
            f"attribute '{key}' has a 'fields' that is not a non-empty object"
        )

    fields: dict[str, AttributeField] = {}
    for field_key, spec in raw.items():
        where = f"attribute '{key}' field '{field_key}'"
        if not isinstance(field_key, str) or not _FIELD_KEY.match(field_key):
            raise ProfileError(
                f"{where}: name must be lower case letters, digits and underscores"
            )
        if not isinstance(spec, dict):
            raise ProfileError(f"{where} must be an object")

        bit, width = _parse_bits(where, spec)

        platform = spec.get("platform", "sensor")
        if platform not in ("sensor", "binary_sensor"):
            raise ProfileError(
                f"{where} has platform {platform!r}; expected sensor or "
                "binary_sensor. To leave a field out, omit it."
            )

        signed = spec.get("signed", False)
        if not isinstance(signed, bool):
            raise ProfileError(f"{where} has a non-boolean signed")
        if signed and width < 2:
            raise ProfileError(f"{where} is one bit wide, so it cannot be signed")

        device_class = spec.get("device_class")
        unit = spec.get("unit")
        if platform == "binary_sensor":
            for name in ("unit", "scale", "offset", "precision", "signed", "values"):
                if spec.get(name) is not None and spec.get(name) is not False:
                    raise ProfileError(
                        f"{where} is a binary sensor, so {name!r} does not apply"
                    )
        elif check_device_class_unit(device_class, unit) is not None:
            raise ProfileError(
                f"{where}: device class '{device_class}' does not accept unit "
                f"{unit!r}; valid units are {allowed_units(device_class)}"
            )

        values = _parse_values(where, spec.get("values"))
        if values is not None and (
            device_class or unit or spec.get("scale") or spec.get("offset")
        ):
            raise ProfileError(
                f"{where} has values as well as a device class, unit, scale or "
                "offset; a labelled state is text and none of those apply"
            )

        scale = spec.get("scale", 1.0)
        offset = spec.get("offset", 0.0)
        for label, number in (("scale", scale), ("offset", offset)):
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                raise ProfileError(f"{where} has a non-numeric {label}")

        precision = spec.get("precision")
        if precision is not None and not isinstance(precision, int):
            raise ProfileError(f"{where} has a non-integer precision")

        fields[field_key] = AttributeField(
            bit=bit,
            width=width,
            signed=signed,
            platform=platform,
            values=values,
            name=spec.get("name"),
            device_class=device_class,
            unit=unit,
            scale=float(scale),
            offset=float(offset),
            precision=precision,
            entity_category=_parse_entity_category(where, spec),
            enabled_by_default=_parse_enabled(where, spec),
        )

    return fields


def _parse_values(where: str, raw: Any) -> dict[str, str] | None:
    """Validate a raw-value-to-label map.

    Keys stay strings. JSON has no integer keys, and the reading they describe
    is an identifier rather than a quantity -- 01 and 1 are not obliged to mean
    the same thing just because they parse to the same number.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ProfileError(f"{where} has a 'values' that is not a non-empty object")

    labels: dict[str, str] = {}
    for value, label in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{where} has an empty value in 'values'")
        if not isinstance(label, str) or not label.strip():
            raise ProfileError(f"{where} has an empty label for value {value!r}")
        labels[value.strip()] = label.strip()
    return labels


def parse_profile(raw: Any) -> DeviceProfile:
    """Build a profile from decoded JSON, raising ProfileError if unusable."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a JSON object")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ProfileError("profile needs a 'name' slug, e.g. teltonika_ftc921")

    display_name = raw.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        raise ProfileError(
            f"profile '{name}' needs a 'display_name', e.g. Teltonika FTC921"
        )

    attributes = raw.get("attributes")
    if not isinstance(attributes, dict) or not attributes:
        raise ProfileError(f"profile '{name}' needs a non-empty 'attributes' object")

    overrides = {key: _parse_attribute(key, value) for key, value in attributes.items()}

    return DeviceProfile(
        name=name,
        display_name=display_name,
        vendor=raw.get("vendor"),
        description=raw.get("description"),
        protocols=tuple(raw.get("protocols") or ()),
        models=tuple(raw.get("models") or ()),
        overrides=overrides,
    )


def load_profiles(directory: Path) -> dict[str, DeviceProfile]:
    """Load every profile in a directory. Blocking; call in an executor."""
    profiles: dict[str, DeviceProfile] = {}
    if not directory.is_dir():
        return profiles

    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            # Documentation and templates, not profiles.
            continue
        try:
            profile = parse_profile(json.loads(path.read_text(encoding="utf-8")))
        except (ProfileError, json.JSONDecodeError, OSError) as err:
            # One bad file must not take the integration down with it.
            LOGGER.error("Ignoring device profile %s: %s", path.name, err)
            continue

        if profile.name in profiles:
            LOGGER.error(
                "Ignoring device profile %s: name '%s' is already used",
                path.name,
                profile.name,
            )
            continue
        profiles[profile.name] = profile

    return profiles


async def async_get_profiles(hass: HomeAssistant) -> dict[str, DeviceProfile]:
    """Return the profile library, reading from disk once per Home Assistant run."""
    if (cached := hass.data.get(_DATA_PROFILES)) is not None:
        return cached

    directory = Path(__file__).parent / PROFILES_DIRECTORY
    profiles = await hass.async_add_executor_job(load_profiles, directory)
    hass.data[_DATA_PROFILES] = profiles
    LOGGER.debug("Loaded %s device profile(s)", len(profiles))
    return profiles


def suggest(
    profiles: dict[str, DeviceProfile],
    protocol: str | None = None,
    model: str | None = None,
) -> list[DeviceProfile]:
    """Return profiles ordered with the likeliest matches first.

    Traccar tells us the protocol a device speaks and, when someone filled it
    in, a free-text model. Neither is reliable enough to pick a profile
    automatically, but both are good enough to save scrolling.
    """
    protocol = (protocol or "").strip().lower()
    model_text = (model or "").strip().lower()

    def rank(profile: DeviceProfile) -> tuple[int, str]:
        score = 2
        if model_text and any(m.lower() in model_text for m in profile.models):
            score = 0
        elif (protocol and protocol in {p.lower() for p in profile.protocols}) or (
            model_text and profile.vendor and profile.vendor.lower() in model_text
        ):
            score = 1
        return score, profile.display_name.lower()

    return sorted(profiles.values(), key=rank)


def match(
    profiles: dict[str, DeviceProfile], model: str | None
) -> DeviceProfile | None:
    """Return the profile that covers a device model, if exactly one does.

    Matching is on the model alone. Protocol is deliberately not used here even
    though it ranks the dropdown: every Teltonika tracker speaks "teltonika",
    so applying a profile on that basis would map one model's IO parameters
    onto another's and silently produce wrong readings.

    Ambiguity yields nothing rather than a guess, leaving the choice to the user.
    """
    if not (wanted := (model or "").strip().lower()):
        return None

    hits = [
        profile
        for profile in profiles.values()
        if any(candidate.strip().lower() == wanted for candidate in profile.models)
    ]
    if len(hits) == 1:
        return hits[0]
    if hits:
        LOGGER.warning(
            "Model %s is claimed by %s profiles (%s); select one on the device",
            model,
            len(hits),
            ", ".join(sorted(p.name for p in hits)),
        )
    return None
