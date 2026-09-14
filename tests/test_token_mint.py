"""Tests for exchanging a Traccar login for an API token.

Both assertions here come from a live install returning
`415 Unsupported Media Type` from `/api/session/token`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import custom_components.traccar_client_extended.config_flow as config_flow
from custom_components.traccar_client_extended.config_flow import (
    TOKEN_EXPIRY,
    TraccarClientExtendedConfigFlow,
)

CREDENTIALS = {
    "host": "traccar.example.com",
    "port": "8082",
    "ssl": True,
    "verify_ssl": True,
    "email": "user@example.com",
    "password": "hunter2",
}


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.status = 200
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self) -> str:
        return self._body

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """Record every POST instead of making one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse("  a-token\n")


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(config_flow, "async_create_clientsession", lambda *a, **k: fake)
    return fake


async def mint(session) -> str:
    flow = object.__new__(TraccarClientExtendedConfigFlow)
    flow.hass = SimpleNamespace()
    return await flow._async_mint_token(CREDENTIALS)


async def test_token_request_sends_a_form_body(session) -> None:
    """`requestToken` declares a @FormParam, so Jersey consumes form-encoded only.

    A POST with no body carries no content type to match, and Traccar answers
    415 -- which is what a real server did.
    """
    await mint(session)

    url, kwargs = session.calls[-1]
    assert url.endswith("/api/session/token")
    assert "expiration" in kwargs["data"]
    assert "json" not in kwargs


async def test_token_outlives_traccars_seven_day_default(session) -> None:
    """Without an expiration Traccar mints a 7-day token.

    That is a browser session's lifetime, not an integration's: it would expire
    a week after setup and surface as a reauth prompt with no obvious cause.
    """
    await mint(session)

    _, kwargs = session.calls[-1]
    expiration = datetime.strptime(kwargs["data"]["expiration"], "%Y-%m-%dT%H:%M:%SZ")
    ahead = expiration.replace(tzinfo=UTC) - datetime.now(UTC)
    assert ahead.days > 365
    assert abs(ahead - TOKEN_EXPIRY).total_seconds() < 60


async def test_login_comes_first_and_the_token_is_stripped(session) -> None:
    """The cookie from /api/session is what authenticates the token request."""
    token = await mint(session)

    assert [url for url, _ in session.calls] == [
        "https://traccar.example.com:8082/api/session",
        "https://traccar.example.com:8082/api/session/token",
    ]
    assert session.calls[0][1]["data"]["email"] == "user@example.com"
    assert token == "a-token"
