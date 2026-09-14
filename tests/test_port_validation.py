"""Tests for the port field, which is free text and so has to be checked.

It is a `TextSelector` rather than a `NumberSelector` because the number
control renders 8082 as "8,082". Nothing then validates it, and what was typed
is stored verbatim -- so an empty or misspelled box produced
`http://host:/api`, which is the base of every URL in `coordinator._api` and of
the configuration link on each device page. pytraccar falls back to 8082 for
its own base url, so REST kept working and nothing else did.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.traccar_client_extended.config_flow import (
    TraccarClientExtendedConfigFlow,
    _clean_port,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("8082", 8082), (8082, 8082), ("  443 ", 443), ("1", 1), ("65535", 65535)],
)
def test_a_usable_port_becomes_an_int(raw, expected) -> None:
    """Whatever the form hands over, the entry stores a number."""
    assert _clean_port(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "80 82", "eighty", None, "0", "65536", "-1"]
)
def test_an_unusable_port_is_refused(raw) -> None:
    """Including the empty box, which is the one that actually happens."""
    with pytest.raises(vol.Invalid):
        _clean_port(raw)


async def test_the_token_step_reports_a_bad_port_on_the_field() -> None:
    """Refused before the connection attempt, so the error names the cause.

    Left to `_get_server_info`, an empty port reaches pytraccar, which falls
    back to 8082 -- so the form would have accepted it and the damage would
    only appear later, in the notification check and the device links.
    """
    flow = object.__new__(TraccarClientExtendedConfigFlow)
    flow.hass = SimpleNamespace()
    shown: dict = {}
    flow.async_show_form = lambda **kwargs: shown.update(kwargs) or kwargs

    result = await flow.async_step_token(
        {
            "host": "traccar.example.com",
            "port": "",
            "api_token": "tok",
            "ssl": False,
            "verify_ssl": True,
        }
    )

    assert result["errors"] == {"port": "invalid_port"}
    assert result["step_id"] == "token"
