"""Tests for network operator decoding and the shipped operator library.

The library is generated rather than hand-written, which moves the risk: a bad
regeneration would not be caught by review, because nobody reads 238 files of
diff. These tests are what does read them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.traccar_client_extended.operators import (
    OPERATORS_DIRECTORY,
    OperatorError,
    load_operators,
    operator_name,
    parse_operators,
)

OPERATORS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "traccar_client_extended"
    / OPERATORS_DIRECTORY
)


def operator_files() -> list[Path]:
    """Every shipped file, excluding underscore-prefixed documentation."""
    return [
        p for p in sorted(OPERATORS_PATH.glob("*.json")) if not p.name.startswith("_")
    ]


# -- The shipped library ------------------------------------------------------


def test_library_is_not_empty() -> None:
    """A missing generation step would leave the feature silently inert."""
    assert len(operator_files()) > 100


def test_every_file_parses() -> None:
    """Every shipped file passes the loader's own validation.

    One test over all of them rather than one per file: 238 parametrised ids
    would be most of the suite, and naming the file in the message is the only
    thing parametrising would have bought.
    """
    for path in operator_files():
        try:
            _, names = parse_operators(json.loads(path.read_text(encoding="utf-8")))
        except (OperatorError, json.JSONDecodeError) as err:
            pytest.fail(f"{path.name}: {err}")
        assert names, f"{path.name}: no operators"


def test_file_names_match_their_mcc() -> None:
    """Lookup keys on the `mcc` field, so a mismatch hides a whole country."""
    for path in operator_files():
        mcc, _ = parse_operators(json.loads(path.read_text(encoding="utf-8")))
        assert mcc == path.stem, f"{path.name} declares mcc {mcc}"


def test_library_loads() -> None:
    """The real loader reads the real directory."""
    library = load_operators(OPERATORS_PATH)
    assert library
    assert all(len(mcc) == 3 for mcc in library)


def test_template_is_not_loaded() -> None:
    """Underscore-prefixed files are documentation."""
    assert (OPERATORS_PATH / "_TEMPLATE.json").exists()
    assert "000" not in load_operators(OPERATORS_PATH)


def test_known_codes_resolve() -> None:
    """Spot checks against the shipped data, in three countries."""
    library = load_operators(OPERATORS_PATH)
    assert operator_name(library, 302220) == "Telus Mobility"
    assert operator_name(library, "310260") == "T-Mobile"
    assert operator_name(library, 23430) == "EE"


def test_missing_directory_is_not_fatal() -> None:
    assert load_operators(OPERATORS_PATH / "nope") == {}


def test_one_bad_file_does_not_take_the_rest(tmp_path: Path) -> None:
    (tmp_path / "302.json").write_text(
        json.dumps({"mcc": "302", "operators": {"220": "Telus"}}), encoding="utf-8"
    )
    (tmp_path / "310.json").write_text("{ not json", encoding="utf-8")
    assert load_operators(tmp_path) == {"302": {"220": "Telus"}}


def test_duplicate_mcc_is_refused(tmp_path: Path) -> None:
    """Two files claiming one MCC would silently shadow each other."""
    (tmp_path / "a.json").write_text(
        json.dumps({"mcc": "302", "operators": {"220": "First"}}), encoding="utf-8"
    )
    (tmp_path / "b.json").write_text(
        json.dumps({"mcc": "302", "operators": {"220": "Second"}}), encoding="utf-8"
    )
    assert load_operators(tmp_path) == {"302": {"220": "First"}}


# -- Validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"operators": {"220": "Telus"}},
        {"mcc": 302, "operators": {"220": "Telus"}},
        {"mcc": "3020", "operators": {"220": "Telus"}},
        {"mcc": "302"},
        {"mcc": "302", "operators": {}},
        {"mcc": "302", "operators": {"2200": "Telus"}},
        {"mcc": "302", "operators": {"2": "Telus"}},
        {"mcc": "302", "operators": {"220": ""}},
        {"mcc": "302", "operators": {"220": 17}},
    ],
)
def test_invalid_files_are_refused(raw: object) -> None:
    with pytest.raises(OperatorError):
        parse_operators(raw)


def test_leading_zeros_survive() -> None:
    """MNC 01 and MNC 1 are different assignments."""
    _, names = parse_operators({"mcc": "262", "operators": {"01": "Telekom"}})
    assert names == {"01": "Telekom"}


# -- Lookup -------------------------------------------------------------------

LIBRARY = {"302": {"220": "Telus Mobility", "22": "Someone else"}, "262": {"01": "T"}}


def test_splits_on_length() -> None:
    assert operator_name(LIBRARY, "302220") == "Telus Mobility"
    assert operator_name(LIBRARY, "26201") == "T"


def test_accepts_an_integer() -> None:
    """Traccar stores whatever the decoder produced, and that is usually an int."""
    assert operator_name(LIBRARY, 302220) == "Telus Mobility"


def test_truncated_mnc_falls_back() -> None:
    """Some modems report 30222 for what the tables call 302-220."""
    assert (
        operator_name({"302": {"220": "Telus Mobility"}}, "30222") == "Telus Mobility"
    )


def test_exact_match_beats_the_truncation_fallback() -> None:
    """The fallback must never shadow a real two-digit MNC."""
    assert operator_name(LIBRARY, "30222") == "Someone else"


@pytest.mark.parametrize(
    "code",
    [None, True, False, "", "Telus", "30", "3022201", "302-220", 0, 12.5, {"a": 1}],
)
def test_unresolvable_returns_none(code: object) -> None:
    """Every one of these must leave the caller showing the raw value."""
    assert operator_name(LIBRARY, code) is None


def test_unknown_country_and_unknown_network() -> None:
    assert operator_name(LIBRARY, "99999") is None
    assert operator_name(LIBRARY, "302999") is None
