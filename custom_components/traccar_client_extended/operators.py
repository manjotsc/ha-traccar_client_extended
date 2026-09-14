"""Network operator names for the ``operator`` attribute.

Traccar stores whatever the protocol decoder extracted, and for most trackers
that is the numeric PLMN identifier rather than a name: Teltonika's AVL
parameter 241, "Active GSM Operator", arrives as ``302220`` and is written to
``KEY_OPERATOR`` verbatim. Nothing upstream of this integration knows that the
code means Telus.

Decoding it is not a judgement about the data -- it is the same fact in a form
a person can read, exactly like the knots-to-km/h and milliseconds-to-hours
conversions in `attributes`. So it applies everywhere and is not an option.
It sits *below* the override stack rather than inside it: a PLMN code means the
same thing on every Traccar server, so there is nothing for a user to express.

Files live in ``operators/`` beside this module, one per mobile country code,
so contributing a correction is adding three lines of JSON rather than writing
code. Files whose name starts with an underscore are documentation and are not
loaded.

The name is the *network*, never the plan: 302-220 carries Telus, Koodo and
Public Mobile subscribers alike, and the PLMN cannot tell them apart. The raw
code is always published alongside the name as the ``traccar_operator`` state
attribute, so nothing is lost and an unrecognised code still shows up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER

OPERATORS_DIRECTORY = "operators"

_DATA_OPERATORS = f"{DOMAIN}_operators"

#: ``{mcc: {mnc: name}}``. Both keys keep their leading zeros, so they are
#: strings rather than ints -- MNC 01 and MNC 1 are not the same assignment.
type OperatorLibrary = dict[str, dict[str, str]]

_MCC = re.compile(r"^\d{3}$")
_MNC = re.compile(r"^\d{2,3}$")


class OperatorError(ValueError):
    """Raised when an operator file is not usable."""


def parse_operators(raw: Any) -> tuple[str, dict[str, str]]:
    """Validate one operator file, returning its MCC and its MNC map."""
    if not isinstance(raw, dict):
        raise OperatorError("file must be an object")

    mcc = raw.get("mcc")
    if not isinstance(mcc, str) or not _MCC.match(mcc):
        raise OperatorError(f"mcc must be three digits as a string, got {mcc!r}")

    operators = raw.get("operators")
    if not isinstance(operators, dict) or not operators:
        raise OperatorError("operators must be a non-empty object")

    names: dict[str, str] = {}
    for mnc, name in operators.items():
        if not isinstance(mnc, str) or not _MNC.match(mnc):
            raise OperatorError(
                f"mnc must be two or three digits as a string, got {mnc!r}"
            )
        if not isinstance(name, str) or not name.strip():
            raise OperatorError(f"operator {mcc}-{mnc} has an empty name")
        names[mnc] = name.strip()

    return mcc, names


def load_operators(directory: Path) -> OperatorLibrary:
    """Load every operator file in a directory. Blocking; call in an executor."""
    library: OperatorLibrary = {}
    if not directory.is_dir():
        return library

    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            # Documentation and templates, not data.
            continue
        try:
            mcc, names = parse_operators(json.loads(path.read_text(encoding="utf-8")))
        except (OperatorError, json.JSONDecodeError, OSError) as err:
            # One bad file must not cost every other country its names.
            LOGGER.error("Ignoring operator file %s: %s", path.name, err)
            continue

        if mcc in library:
            LOGGER.error(
                "Ignoring operator file %s: MCC %s is already defined", path.name, mcc
            )
            continue
        library[mcc] = names

    return library


async def async_get_operators(hass: HomeAssistant) -> OperatorLibrary:
    """Return the operator library, reading from disk once per Home Assistant run.

    Read in one pass at setup rather than lazily per country, because the
    lookup happens on the state-read path and that runs in the event loop.
    """
    if (cached := hass.data.get(_DATA_OPERATORS)) is not None:
        return cached

    directory = Path(__file__).parent / OPERATORS_DIRECTORY
    library = await hass.async_add_executor_job(load_operators, directory)
    hass.data[_DATA_OPERATORS] = library
    LOGGER.debug(
        "Loaded %s network operator name(s) across %s country code(s)",
        sum(len(names) for names in library.values()),
        len(library),
    )
    return library


def operator_name(library: OperatorLibrary, code: Any) -> str | None:
    """Return the network name for a PLMN code, or None if it is not known.

    ``None`` covers every case the caller should fall back to the raw value
    for: an unlisted code, a decoder that already reported a name as text, and
    a modem that reported nothing.
    """
    if code is None or isinstance(code, bool):
        return None

    digits = str(code).strip()
    # MCC is always three digits and never leading-zero, so the length of what
    # is left is the only thing needed to split a concatenated PLMN.
    if not digits.isdigit() or not 5 <= len(digits) <= 6:
        return None

    mcc, mnc = digits[:3], digits[3:]
    if (network := library.get(mcc)) is None:
        return None
    if (name := network.get(mnc)) is not None:
        return name

    # Some modems truncate a three-digit MNC to two -- Telus reports both
    # 302220 and 30222, which is why AOSP's carrier list carries both forms.
    # Only consulted on a miss, so it can never shadow a real two-digit MNC.
    if len(mnc) == 2:
        return network.get(f"{mnc}0")
    return None
