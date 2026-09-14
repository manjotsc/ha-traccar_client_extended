"""Tests for event publication.

Events reach the integration by two routes -- pushed over the websocket as
Traccar emits them, and re-fetched by the optional backfill poll -- so the
deduplication between them is the thing worth pinning down.
"""

from __future__ import annotations

from custom_components.traccar_client_extended.const import DOMAIN

from .test_coordinator_logic import make_coordinator


class _Bus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, payload: dict) -> None:
        self.fired.append((event_type, payload))


class _Hass:
    """The slice of HomeAssistant that dispatcher and bus calls actually touch."""

    def __init__(self) -> None:
        self.bus = _Bus()
        # async_dispatcher_send_internal returns early when this is unset, so
        # signals go nowhere and only the bus events need asserting on.
        self.data: dict = {}

    def verify_event_loop_thread(self, _what: str) -> None:
        """No-op: these tests run synchronously, outside an event loop."""


def make_event_coordinator(**overrides):
    """A coordinator wired up far enough to publish events."""
    from collections import OrderedDict

    coordinator = make_coordinator(
        data={
            7: {
                "device": {"id": 7, "name": "Van"},
                "position": {},
                "geofences": [],
                "attributes": {},
            }
        },
        **overrides,
    )
    coordinator.hass = _Hass()
    coordinator.events = overrides.get("events", [])
    coordinator._seen_event_ids = OrderedDict()
    coordinator._geofences = []
    return coordinator


def event(event_id: int, event_type: str = "deviceOverspeed") -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "deviceId": 7,
        "eventTime": "2026-02-03T10:00:00.000+00:00",
        "attributes": {},
    }


def fired(coordinator) -> list[str]:
    return [name for name, _ in coordinator.hass.bus.fired]


def test_event_is_published_to_the_bus() -> None:
    """Bus events carry this integration's domain prefix, unlike core's."""
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1))
    assert fired(coordinator) == [f"{DOMAIN}_device_overspeed"]


def test_duplicate_event_id_is_published_once() -> None:
    """The websocket and the backfill poll both deliver the same event."""
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1))
    coordinator.publish_event(event(1))
    assert len(fired(coordinator)) == 1


def test_distinct_events_both_publish() -> None:
    """Deduplication must not swallow genuinely new events."""
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1))
    coordinator.publish_event(event(2))
    assert len(fired(coordinator)) == 2


def test_events_without_an_id_are_not_deduplicated() -> None:
    """Publishing a rare duplicate beats dropping a real event."""
    coordinator = make_event_coordinator()
    payload = event(1)
    del payload["id"]
    coordinator.publish_event(payload)
    coordinator.publish_event(payload)
    assert len(fired(coordinator)) == 2


def test_seen_ids_are_bounded() -> None:
    """The dedup memory must not grow without limit."""
    from custom_components.traccar_client_extended.coordinator import _MAX_SEEN_EVENTS

    coordinator = make_event_coordinator()
    for event_id in range(_MAX_SEEN_EVENTS + 50):
        coordinator.publish_event(event(event_id))
    assert len(coordinator._seen_event_ids) <= _MAX_SEEN_EVENTS


def test_user_selection_filters_pushed_events() -> None:
    """The websocket sends every type; the user's choice still applies."""
    coordinator = make_event_coordinator(events=["geofenceEnter"])
    coordinator.publish_event(event(1, "deviceOverspeed"))
    assert fired(coordinator) == []

    coordinator.publish_event(event(2, "geofenceEnter"))
    assert fired(coordinator) == [f"{DOMAIN}_geofence_enter"]


def test_empty_selection_allows_every_type() -> None:
    """An empty list means no filter, not 'block everything'."""
    coordinator = make_event_coordinator(events=[])
    coordinator.publish_event(event(1, "ignitionOn"))
    assert fired(coordinator) == [f"{DOMAIN}_ignition_on"]


def test_unknown_event_type_is_ignored() -> None:
    """A type Traccar adds later must not raise."""
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1, "somethingNew"))
    assert fired(coordinator) == []


def test_event_for_unknown_device_is_ignored() -> None:
    """Events can arrive for a device we have not fetched yet."""
    coordinator = make_event_coordinator()
    payload = event(1)
    payload["deviceId"] = 999
    coordinator.publish_event(payload)
    assert fired(coordinator) == []


def test_payload_shape() -> None:
    """Automations depend on these keys."""
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1))
    _, payload = coordinator.hass.bus.fired[0]
    assert payload == {
        "device_traccar_id": 7,
        "device_name": "Van",
        "type": "deviceOverspeed",
        "alarm": None,
        "message": None,
        "server_time": "2026-02-03T10:00:00.000+00:00",
        "position_id": None,
        "geofence_id": None,
        "geofence": None,
        "maintenance_id": None,
        "attributes": {},
    }


# -- Alarms -------------------------------------------------------------------


def test_alarm_is_republished_under_its_specific_type() -> None:
    """Traccar reports a tow and a hard braking event with the same type.

    Both arrive as `alarm` with the detail in attributes, so without this an
    automation cannot tell a theft from a driving-style event.
    """
    coordinator = make_event_coordinator()
    payload = event(1, "alarm")
    payload["attributes"] = {"alarm": "tow"}
    coordinator.publish_event(payload)

    assert fired(coordinator) == [f"{DOMAIN}_alarm", f"{DOMAIN}_alarm_tow"]


def test_alarm_payload_surfaces_the_alarm() -> None:
    coordinator = make_event_coordinator()
    payload = event(1, "alarm")
    payload["attributes"] = {"alarm": "tow"}
    coordinator.publish_event(payload)
    assert coordinator.hass.bus.fired[0][1]["alarm"] == "tow"


def test_camel_case_alarms_become_snake_case() -> None:
    coordinator = make_event_coordinator()
    payload = event(1, "alarm")
    payload["attributes"] = {"alarm": "hardBraking"}
    coordinator.publish_event(payload)
    assert f"{DOMAIN}_alarm_hard_braking" in fired(coordinator)


def test_unknown_alarm_falls_back_to_the_generic_event() -> None:
    """A vendor alarm Traccar has not defined must not be dropped."""
    coordinator = make_event_coordinator()
    payload = event(1, "alarm")
    payload["attributes"] = {"alarm": "somethingNew"}
    coordinator.publish_event(payload)
    assert fired(coordinator) == [f"{DOMAIN}_alarm"]


def test_alarm_without_a_subtype_fires_once() -> None:
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1, "alarm"))
    assert fired(coordinator) == [f"{DOMAIN}_alarm"]


# -- Coverage of Traccar's event types ---------------------------------------


def test_every_traccar_event_type_is_mapped() -> None:
    """An unmapped type is silently dropped, which is how eight went missing."""
    from custom_components.traccar_client_extended.const import EVENTS

    for event_type in (
        "unaccompaniedMotion",
        "deviceInactive",
        "deviceFuelIncrease",
        "geofenceCrossed",
        "proximityEnter",
        "proximityExit",
        "queuedCommandSent",
        "media",
    ):
        assert event_type in EVENTS, event_type


def test_theft_relevant_events_reach_the_bus() -> None:
    coordinator = make_event_coordinator()
    coordinator.publish_event(event(1, "unaccompaniedMotion"))
    assert fired(coordinator) == [f"{DOMAIN}_unaccompanied_motion"]


def test_entity_advertises_alarm_types() -> None:
    """So an automation can pick `alarm_tow` from the trigger UI."""
    from custom_components.traccar_client_extended.const import EVENT_TYPES

    assert "alarm_tow" in EVENT_TYPES
    assert "alarm_jamming" in EVENT_TYPES
    assert "unaccompanied_motion" in EVENT_TYPES


# -- Detail Traccar sends alongside the event --------------------------------


def test_message_is_surfaced() -> None:
    """NotificatorWeb formats a readable digest; do not bury it in attributes."""
    coordinator = make_event_coordinator()
    payload = event(1, "deviceOverspeed")
    payload["attributes"] = {
        "message": "Mock exceeds the speed 37.0 km/h at 2026-09-07 16:07:07",
        "speed": 20.0,
        "speedLimit": 10.0,
    }
    coordinator.publish_event(payload)

    data = coordinator.hass.bus.fired[0][1]
    assert data["message"].startswith("Mock exceeds the speed")
    # The structured detail stays available too.
    assert data["attributes"]["speed"] == 20.0
    assert data["attributes"]["speedLimit"] == 10.0


def test_geofence_is_resolved_to_its_name() -> None:
    """A geofence id alone is useless in an automation."""
    coordinator = make_event_coordinator()
    coordinator._geofences = [{"id": 3, "name": "Home"}, {"id": 4, "name": "Depot"}]
    payload = event(1, "geofenceExit")
    payload["geofenceId"] = 4
    coordinator.publish_event(payload)

    data = coordinator.hass.bus.fired[0][1]
    assert data["geofence_id"] == 4
    assert data["geofence"] == "Depot"


def test_unknown_geofence_id_is_not_fatal() -> None:
    coordinator = make_event_coordinator()
    coordinator._geofences = []
    payload = event(1, "geofenceExit")
    payload["geofenceId"] = 99
    coordinator.publish_event(payload)
    assert coordinator.hass.bus.fired[0][1]["geofence"] is None
