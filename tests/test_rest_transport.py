"""Tests for the raw REST helper and the cookie jar the websocket depends on.

Both cases here are silent failures on a correctly configured server, which is
why they are worth pinning: nothing raises, nothing logs an error, and the
symptom appears somewhere else entirely.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import CookieJar
from yarl import URL

import custom_components.traccar_client_extended as integration
import custom_components.traccar_client_extended.config_flow as config_flow
from custom_components.traccar_client_extended.config_flow import (
    TraccarClientExtendedConfigFlow,
)

from .test_coordinator_logic import make_coordinator


class FakeResponse:
    """Just enough of aiohttp's response for `_api` to read."""

    def __init__(self, body: bytes, *, status: int = 200, length: int | None = None):
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}
        # aiohttp reports None whenever the Content-Length header is absent,
        # which is every chunked response.
        self.content_length = length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        return None

    async def read(self) -> bytes:
        return self._body

    async def json(self):  # pragma: no cover - _api must not reach for this
        raise AssertionError("the body is read, not parsed by content type")


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self._response


def api_coordinator(response: FakeResponse):
    """A coordinator whose session returns one canned response."""
    coordinator = make_coordinator()
    coordinator._session = FakeSession(response)
    coordinator._token = "tok"
    coordinator._verify_ssl = True
    coordinator.server_url = "http://traccar.example.com:8082"
    coordinator._clock_offset = None
    return coordinator


async def test_chunked_response_body_is_not_discarded() -> None:
    """A body with no Content-Length is still the body.

    Any reverse proxy that gzips answers chunked, so the header is absent on
    most real deployments. Testing `not response.content_length` therefore threw
    the notification list away and left `async_notifications` returning `[]` --
    which reads here as "no web notification exists", raising the repair on a
    correctly configured server and, with `create_notifications` on, creating
    all 23 notifications again on every setup.
    """
    body = json.dumps([{"id": 1, "type": "alarm", "notificators": "web"}]).encode()
    coordinator = api_coordinator(FakeResponse(body, length=None))

    assert await coordinator._api("notifications") == [
        {"id": 1, "type": "alarm", "notificators": "web"}
    ]


async def test_no_content_is_none() -> None:
    """A 204 carries nothing, and neither does an empty 200."""
    assert await api_coordinator(FakeResponse(b"", status=204))._api("x") is None
    assert await api_coordinator(FakeResponse(b"   ", length=3))._api("x") is None


async def test_a_non_json_body_is_none_rather_than_an_exception() -> None:
    """A proxy error page must not take the delivery check down with it."""
    coordinator = api_coordinator(FakeResponse(b"<html>502</html>", length=16))
    assert await coordinator._api("notifications") is None


@pytest.mark.parametrize(
    ("ssl", "verify_ssl"),
    [(False, True), (True, True), (True, False)],
)
async def test_the_cookie_jar_is_always_unsafe(monkeypatch, ssl, verify_ssl) -> None:
    """The JSESSIONID has to survive a bare IP host however TLS is configured.

    aiohttp's default jar drops cookies from an address rather than a name, and
    /api/socket authenticates with that cookie alone. Gating `unsafe` on the
    connection being unencrypted meant a verified HTTPS endpoint reached by IP
    got working REST and a websocket closed for being unauthenticated, with
    nothing anywhere saying why.
    """
    jars: list[CookieJar] = []

    def _session(hass, cookie_jar=None, **kwargs):
        jars.append(cookie_jar)
        raise _Stop

    monkeypatch.setattr(integration, "async_create_clientsession", _session)
    monkeypatch.setattr(
        integration,
        "async_get_loaded_integration",
        lambda *a: SimpleNamespace(version="1"),
    )

    entry = SimpleNamespace(
        domain="traccar_client_extended",
        data={
            "host": "192.168.1.10",
            "port": 8082,
            "api_token": "tok",
            "ssl": ssl,
            "verify_ssl": verify_ssl,
        },
    )
    with pytest.raises(_Stop):
        await integration.async_setup_entry(SimpleNamespace(), entry)

    assert jars and stores_a_cookie_for_an_ip(jars[0])


async def test_the_token_mint_jar_is_always_unsafe(monkeypatch) -> None:
    """Same cookie, same rule, on the flow that mints a token from a login."""
    jars: list[CookieJar] = []

    def _session(hass, cookie_jar=None, **kwargs):
        jars.append(cookie_jar)
        raise _Stop

    monkeypatch.setattr(config_flow, "async_create_clientsession", _session)

    flow = object.__new__(TraccarClientExtendedConfigFlow)
    flow.hass = SimpleNamespace()
    with pytest.raises(_Stop):
        await flow._async_mint_token(
            {
                "host": "192.168.1.10",
                "port": 8082,
                "ssl": True,
                "verify_ssl": True,
                "email": "a@b.c",
                "password": "x",
            }
        )

    assert jars and stores_a_cookie_for_an_ip(jars[0])


def stores_a_cookie_for_an_ip(jar: CookieJar) -> bool:
    """Ask the jar the question the websocket asks it, rather than read a flag.

    `CookieJar` exposes `unsafe` only as a private attribute, and what actually
    matters is the behaviour: Traccar is usually reached by address, so a jar
    that will not keep a cookie for one cannot authenticate /api/socket.
    """
    jar.update_cookies({"JSESSIONID": "abc"}, URL("http://192.168.1.10:8082/"))
    return len(jar) == 1


class _Stop(Exception):
    """Cut a setup short once the thing under test has been built."""
