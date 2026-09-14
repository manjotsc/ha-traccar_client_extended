"""Tests for latency triage.

The integration adds no measurable delay of its own, which made "why is this
event six seconds old" unanswerable from the Home Assistant side: the logs
recorded that a payload arrived and what we did with it, never what Traccar said
about when it happened. `_latency_report` closes that, and these tests pin the
arithmetic it reports, because a sign error here would point the blame at the
wrong host.

"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.traccar_client_extended.coordinator import (
    _EVENT_STAMPS,
    _POSITION_STAMPS,
    _diagnose_fixed_delay,
    _latency_report,
)

from .test_coordinator_logic import make_coordinator

NOW = datetime(2026, 9, 7, 20, 17, 16, tzinfo=UTC)


def at(seconds_ago: float) -> str:
    """A Traccar-style ISO timestamp the given number of seconds before NOW."""
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


# -- Latency report ----------------------------------------------------------


def test_reports_each_stamp_as_a_signed_age() -> None:
    """The three position stamps split the delay into attributable segments.

    Only the serverTime figure is time Traccar and the network are answerable
    for; the older two happened before Traccar saw the position at all.
    """
    position = {
        "deviceTime": at(5.2),
        "fixTime": at(5.2),
        "serverTime": at(4.1),
    }
    report = _latency_report(position, _POSITION_STAMPS, NOW)
    assert report == "deviceTime +5.2s, fixTime +5.2s, serverTime +4.1s"


def test_stamps_are_reported_in_pipeline_order() -> None:
    """Reading order has to match the order Traccar applies them."""
    assert _POSITION_STAMPS == ("deviceTime", "fixTime", "serverTime")


def test_future_stamp_reads_as_negative_not_as_fast_delivery() -> None:
    """A stamp ahead of Home Assistant is clock skew, not a quick push.

    Rendering it unsigned would show a two-second skew as a two-second delay
    and send someone looking for a bottleneck that does not exist.
    """
    assert _latency_report({"eventTime": at(-2.0)}, _EVENT_STAMPS, NOW) == (
        "eventTime -2.0s"
    )


def test_naive_stamp_is_read_as_utc() -> None:
    """An older server may omit the offset; local time would be hours out."""
    naive = (NOW - timedelta(seconds=3)).replace(tzinfo=None).isoformat()
    assert (
        _latency_report({"eventTime": naive}, _EVENT_STAMPS, NOW) == "eventTime +3.0s"
    )


def test_unusable_stamps_are_skipped_not_guessed() -> None:
    """Missing, null and unparseable stamps must not invent a number."""
    payload = {"deviceTime": None, "fixTime": "", "serverTime": "not a timestamp"}
    assert _latency_report(payload, _POSITION_STAMPS, NOW) == "no usable timestamp"


def test_partial_stamps_report_what_is_there() -> None:
    """A position missing deviceTime still yields the segment that matters."""
    payload = {"serverTime": at(4.1)}
    assert _latency_report(payload, _POSITION_STAMPS, NOW) == "serverTime +4.1s"


def test_report_is_skipped_when_debug_is_off() -> None:
    """handle_subscription_data passes None rather than sampling a clock.

    This runs per position per device, so the cost has to vanish entirely when
    nobody is reading the output.
    """
    assert _latency_report({"serverTime": at(4.1)}, _POSITION_STAMPS, None) == "unknown"


# -- Telling a timer from a bottleneck ---------------------------------------
#
# Traccar's server.buffering.threshold holds every position for a fixed interval
# and defaults to 3000 ms, so every stock install pays it. Nothing on the server
# reports it and no API can change it, so the only way to tell a user is to
# measure it -- and the measurement has to be certain enough to be worth acting
# on, because the advice is "edit a config file and restart".


def lags(*values: float) -> list[float]:
    """A sample window, padded to the minimum the detector will act on."""
    padded = list(values)
    while len(padded) < 20:
        padded.extend(values)
    return padded[:20]


def test_a_steady_three_second_lag_is_reported() -> None:
    """The real measurement from a stock server, which is what this is for."""
    verdict = _diagnose_fixed_delay(lags(3.0, 3.1, 3.1, 3.2, 3.3), clock_offset=0.0)

    assert verdict is not None
    median, spread = verdict
    assert 3.0 <= median <= 3.3
    assert spread < 0.5


def test_a_scattered_lag_is_not_reported() -> None:
    """Contention is not buffering, and the fix for buffering would not help it.

    Telling someone with an overloaded server to change a reordering threshold
    sends them to edit the wrong thing, so a wide spread says nothing at all.
    """
    assert (
        _diagnose_fixed_delay(lags(0.4, 6.0, 1.2, 9.5, 2.1), clock_offset=0.0) is None
    )


def test_a_fast_clock_is_not_mistaken_for_a_slow_server() -> None:
    """Skew and delay are the same number until one is measured separately.

    A host running three seconds fast makes every position look three seconds
    old. Without the correction this is exactly the false positive that would
    send someone to edit a server that is behaving perfectly.
    """
    assert _diagnose_fixed_delay(lags(3.0, 3.1, 3.2), clock_offset=3.0) is None


def test_wildly_disagreeing_clocks_report_nothing() -> None:
    """Past a point the offset itself is untrustworthy, so nothing is claimed."""
    assert _diagnose_fixed_delay(lags(30.0, 30.1), clock_offset=27.0) is None


def test_a_lowered_threshold_stops_being_reported() -> None:
    """The measured result of setting the key to 500: about a second, and quiet."""
    assert _diagnose_fixed_delay(lags(0.8, 1.0, 1.0, 1.2), clock_offset=0.0) is None


def test_too_few_samples_report_nothing() -> None:
    """One slow position is not a pattern."""
    assert _diagnose_fixed_delay([3.1, 3.2, 3.0], clock_offset=0.0) is None


def test_skew_correction_can_also_reveal_a_delay() -> None:
    """A slow *Home Assistant* clock hides a real delay, and must not."""
    verdict = _diagnose_fixed_delay(lags(0.1, 0.2, 0.1), clock_offset=-3.0)

    assert verdict is not None
    assert verdict[0] >= 3.0


# -- Learning the offset ------------------------------------------------------


def test_clock_offset_is_read_from_the_date_header() -> None:
    """Every REST response carries one, so no extra request is needed."""
    coordinator = make_coordinator(_clock_offset=None)
    coordinator._record_clock_offset("Wed, 21 Oct 2015 07:28:00 GMT")

    assert coordinator._clock_offset is not None


def test_a_missing_or_broken_date_header_leaves_the_offset_unknown() -> None:
    """Unknown must stay unknown: assuming zero skew invents a false positive."""
    coordinator = make_coordinator(_clock_offset=None)

    coordinator._record_clock_offset(None)
    assert coordinator._clock_offset is None

    coordinator._record_clock_offset("not a date")
    assert coordinator._clock_offset is None
