#!/usr/bin/env python3
"""Regenerate ``operators/`` from the mcc-mnc-list package.

One JSON file per mobile country code, mapping MNC to the network's name. The
source is the `mcc-mnc-list` npm package (MIT), which publishes one row per
PLMN assignment with a brand, an operating company and a country -- the shape
this needs. See ``operators/README.md`` for why that source and not another.

Standard library only, so it runs with system Python:

    python tools/generate_operators.py

Hand edits survive nothing: this rewrites every file it generates. A correction
belongs upstream, or in a pull request that the next regeneration is checked
against.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_URL = "https://cdn.jsdelivr.net/npm/mcc-mnc-list/mcc-mnc-list.json"

DESTINATION = Path(__file__).resolve().parent.parent / (
    "custom_components/traccar_client_extended/operators"
)

#: Placeholder names that carry no information. The raw code is a better state
#: than a word that only says the list did not know either.
_PLACEHOLDERS = frozenset(
    {"", "-", "n/a", "na", "none", "unknown", "unassigned", "not known", "?"}
)


def _fetch(url: str) -> list[dict[str, Any]]:
    """Download the source list."""
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _name(row: dict[str, Any]) -> str | None:
    """Pick the name to publish for one PLMN row.

    ``brand`` is the consumer-facing name and is usually what someone wants to
    read on a card. Where a code is shared it holds a list -- 302-220 is
    "Telus Mobility, Koodo Mobile, Public Mobile" -- and a list is worse than
    useless as a state, so the operating company wins there. The PLMN cannot
    distinguish those brands anyway; it identifies the network.
    """
    brand = (row.get("brand") or "").strip()
    operator = (row.get("operator") or "").strip()

    if brand and "," in brand:
        return operator or brand.split(",")[0].strip() or None
    for candidate in (brand, operator):
        if candidate and candidate.lower() not in _PLACEHOLDERS:
            return candidate
    return None


def _better(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Return True if `new` should replace `old` for the same MCC/MNC.

    Duplicates are historical reassignments; the operational one is the one a
    device reporting that code today is actually attached to.
    """
    return new.get("status") == "Operational" and old.get("status") != "Operational"


def build(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group source rows into one file payload per MCC."""
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    countries: dict[str, Counter[str]] = {}

    for row in rows:
        mcc = (row.get("mcc") or "").strip()
        mnc = (row.get("mnc") or "").strip()
        if not (mcc.isdigit() and len(mcc) == 3):
            continue
        if not (mnc.isdigit() and 2 <= len(mnc) <= 3):
            continue
        if _name(row) is None:
            continue

        key = (mcc, mnc)
        if key not in chosen or _better(row, chosen[key]):
            chosen[key] = row
        if country := (row.get("countryName") or "").strip():
            countries.setdefault(mcc, Counter())[country] += 1

    files: dict[str, dict[str, Any]] = {}
    for (mcc, mnc), row in sorted(chosen.items(), key=lambda item: _sort_key(item[0])):
        payload = files.setdefault(mcc, {"mcc": mcc, "operators": {}})
        payload["operators"][mnc] = _name(row)

    for mcc, payload in files.items():
        if counted := countries.get(mcc):
            # Placed after "mcc" and before "operators" for readability; dicts
            # keep insertion order, so rebuild rather than assign.
            files[mcc] = {
                "mcc": mcc,
                "country": counted.most_common(1)[0][0],
                "operators": payload["operators"],
            }

    return files


def _sort_key(pair: tuple[str, str]) -> tuple[int, int, str]:
    """Sort MCC/MNC numerically, keeping equal-value MNCs stable by text."""
    mcc, mnc = pair
    return int(mcc), int(mnc), mnc


def write(files: dict[str, dict[str, Any]], destination: Path) -> int:
    """Write one file per MCC, replacing what is there. Returns the file count."""
    destination.mkdir(parents=True, exist_ok=True)

    for path in destination.glob("*.json"):
        if not path.name.startswith("_"):
            path.unlink()

    for mcc, payload in sorted(files.items()):
        path = destination / f"{mcc}.json"
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    return len(files)


def main() -> int:
    """Fetch, group and write."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SOURCE_URL, help="source list URL")
    parser.add_argument("--out", type=Path, default=DESTINATION, help="output folder")
    args = parser.parse_args()

    print(f"Fetching {args.url}")
    rows = _fetch(args.url)
    print(f"  {len(rows)} PLMN rows")

    files = build(rows)
    total = sum(len(payload["operators"]) for payload in files.values())
    count = write(files, args.out)
    print(f"Wrote {count} file(s), {total} operator name(s), to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
