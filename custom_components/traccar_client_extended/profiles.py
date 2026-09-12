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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .attributes import AttributeOverride, allowed_units, check_device_class_unit
from .const import DOMAIN, LOGGER

PROFILES_DIRECTORY = "device_profiles"

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

    if platform is None:
        platform = "sensor"

    return AttributeOverride(
        platform=platform,
        device_class=device_class,
        unit=unit,
        scale=float(scale),
        precision=precision,
        name=raw.get("name"),
    )


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
