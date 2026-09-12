"""Tests for the event delivery check.

Traccar pushes an event to a websocket client only through `NotificatorWeb`,
which fires when a Notification with the `web` channel matches. Without one,
every event entity stays silent and nothing anywhere says why — a failure mode
that cost a long debugging session, which is why the integration now checks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.traccar_client_extended.coordinator as coordinator_module
from custom_components.traccar_client_extended.const import (
    EVENTS,
    ISSUE_ALARM_NOTIFICATION_EMPTY,
    ISSUE_NO_WEB_NOTIFICATION,
)

from .test_coordinator_logic import make_coordinator


@pytest.fixture
def issues(monkeypatch):
    """Record repair issues instead of raising them."""
    created: list[str] = []
    deleted: list[str] = []
    kwargs: dict[str, dict] = {}

    def _create(hass, domain, issue_id, **kw):
        created.append(issue_id)
        kwargs[issue_id] = kw

    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", _create)
    monkeypatch.setattr(
        coordinator_module.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append(issue_id),
    )
    return SimpleNamespace(created=created, deleted=deleted, kwargs=kwargs)


def coordinator(notifications, *, fail=False, create=False):
    """A coordinator whose only link to Traccar is a fake `_api`.

    Stubbing at that level rather than at `async_notifications` means the real
    listing and creation code both run, so a change to the notification body is
    caught here rather than only in production.
    """
    c = make_coordinator(create_notifications=create, events=[])
    c.hass = SimpleNamespace()
    c.config_entry = SimpleNamespace(entry_id="abc", title="Traccar", data={})
    c.posted: list[dict] = []

    async def _api(path, method="GET", body=None):
        if fail:
            raise RuntimeError("unreachable")
        if method == "POST":
            c.posted.append(body)
            notifications.append(body)
            return None
        return notifications

    c._api = _api
    return c


def web(event_type: str, alarms: str | None = None) -> dict:
    attributes = {"alarms": alarms} if alarms is not None else {}
    return {"type": event_type, "notificators": "web", "attributes": attributes}


# -- The warning --------------------------------------------------------------


async def test_no_notifications_at_all_raises_the_issue(issues) -> None:
    await coordinator([]).async_check_event_delivery()
    assert issues.created == [f"{ISSUE_NO_WEB_NOTIFICATION}_abc"]


async def test_notifications_on_other_channels_do_not_count(issues) -> None:
    """A mail notification does not make events reach Home Assistant."""
    mail = {"type": "deviceOverspeed", "notificators": "mail", "attributes": {}}
    await coordinator([mail]).async_check_event_delivery()
    assert issues.created == [f"{ISSUE_NO_WEB_NOTIFICATION}_abc"]


async def test_one_web_notification_is_enough(issues) -> None:
    """Having configured one, the user understands the mechanism."""
    await coordinator([web("deviceOverspeed")]).async_check_event_delivery()
    assert issues.created == []
    assert f"{ISSUE_NO_WEB_NOTIFICATION}_abc" in issues.deleted


async def test_multiple_channels_on_one_notification(issues) -> None:
    item = {
        "type": "alarm",
        "notificators": "mail,web",
        "attributes": {"alarms": "tow"},
    }
    await coordinator([item]).async_check_event_delivery()
    assert issues.created == []


async def test_a_notification_is_what_clears_it(issues) -> None:
    """One web notification is enough; the user has chosen their types."""
    await coordinator([web("alarm", alarms="tow")]).async_check_event_delivery()

    assert issues.created == []
    assert f"{ISSUE_NO_WEB_NOTIFICATION}_abc" in issues.deleted


async def test_an_unreachable_server_never_nags(issues) -> None:
    """A version without the endpoint must not produce a config warning."""
    await coordinator([], fail=True).async_check_event_delivery()
    assert issues.created == []
    assert issues.deleted == []


# -- The alarm trap -----------------------------------------------------------


async def test_alarm_notification_without_alarms_is_flagged(issues) -> None:
    """Traccar's filter returns false rather than defaulting to every alarm."""
    await coordinator([web("alarm", alarms=None)]).async_check_event_delivery()
    assert f"{ISSUE_ALARM_NOTIFICATION_EMPTY}_abc" in issues.created


async def test_alarm_notification_with_alarms_is_fine(issues) -> None:
    await coordinator([web("alarm", alarms="tow,sos")]).async_check_event_delivery()
    assert ISSUE_ALARM_NOTIFICATION_EMPTY not in "".join(issues.created)


async def test_non_alarm_notifications_need_no_alarm_list(issues) -> None:
    await coordinator([web("geofenceEnter")]).async_check_event_delivery()
    assert issues.created == []


# -- Creating them ------------------------------------------------------------


async def test_created_notifications_use_the_web_channel() -> None:
    sent: list[dict] = []
    c = coordinator([])

    async def _api(path, method="GET", body=None):
        sent.append({"path": path, "method": method, "body": body})

    c._api = _api
    created = await c.async_create_web_notifications(["deviceOverspeed"])

    assert created == 1
    assert sent[0]["path"] == "notifications"
    assert sent[0]["method"] == "POST"
    assert sent[0]["body"]["notificators"] == "web"
    assert sent[0]["body"]["always"] is True


async def test_created_alarm_notification_lists_every_alarm() -> None:
    """Otherwise the notification we just created would match nothing."""
    sent: list[dict] = []
    c = coordinator([])

    async def _api(path, method="GET", body=None):
        sent.append(body)

    c._api = _api
    await c.async_create_web_notifications(["alarm"])

    alarms = sent[0]["attributes"]["alarms"].split(",")
    assert "tow" in alarms
    assert "jamming" in alarms
    assert len(alarms) > 30


def test_every_type_gets_a_notification_by_default() -> None:
    """Anything less leaves some event types silently undeliverable.

    A partial set is the worst of both: events appear to work, and the two or
    three types nobody created a notification for never arrive, with nothing
    distinguishing them from types that simply have not happened yet.
    """
    c = coordinator([], create=True)

    assert c.wanted_event_types() == sorted(EVENTS)


def test_a_filtered_events_option_narrows_what_is_created() -> None:
    """A notification for a type we would discard on arrival is server litter."""
    c = coordinator([], create=True)
    c.events = ["deviceOverspeed", "alarm"]

    assert c.wanted_event_types() == ["alarm", "deviceOverspeed"]


# -- Doing the server-side setup rather than asking for it --------------------


async def test_setup_creates_the_notifications_when_asked_to(issues) -> None:
    """The point of the checkbox: connect, and events are already instant.

    Without this the user has to find a repair and click it, which is the
    "struggle with Traccar's settings" the integration exists to remove.
    """
    c = coordinator([], create=True)

    await c.async_check_event_delivery()

    assert [item["type"] for item in c.posted] == sorted(EVENTS)
    assert not issues.created


async def test_existing_notifications_are_left_alone(issues) -> None:
    """Someone who configured one has chosen their types; adding is presumptuous."""
    c = coordinator([web("alarm")], create=True)

    await c.async_check_event_delivery()

    assert c.posted == []


async def test_creation_is_not_attempted_when_not_asked_for(issues) -> None:
    """An entry predating the checkbox must never write to the server.

    `create_notifications` is absent from those entries, and defaulting it to
    the form's own default would turn an upgrade into an uninvited write.
    """
    c = coordinator([], create=False)

    await c.async_check_event_delivery()

    assert c.posted == []
    assert issues.created == [f"{ISSUE_NO_WEB_NOTIFICATION}_abc"]


async def test_failed_creation_falls_back_to_the_repair(issues) -> None:
    """A token without permission to manage notifications must still explain itself."""
    c = coordinator([], create=True)

    async def _api(path, method="GET", body=None):
        if method == "POST":
            raise RuntimeError("forbidden")
        return []

    c._api = _api

    await c.async_check_event_delivery()

    assert issues.created == [f"{ISSUE_NO_WEB_NOTIFICATION}_abc"]
