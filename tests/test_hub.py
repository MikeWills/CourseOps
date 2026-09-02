"""Fan-out from the single APRS-IS ingest to connected browsers."""

from __future__ import annotations

import asyncio

from aprswebtracker.hub import QUEUE_MAXSIZE, Hub, position_message
from aprswebtracker.parser import parse_packet

PACKET = "N0CALL-7>APRS,TCPIP*,qAC,X:!3444.00N/08635.00W>180/030/A=001000Rolling"


def run(coro):
    return asyncio.run(coro)


def test_every_subscriber_on_an_event_receives_a_message():
    async def scenario():
        hub = Hub()
        a, b = hub.subscribe(1), hub.subscribe(1)
        await hub.publish(1, {"type": "position", "station_key": "N0CALL-7"})
        return a.queue.get_nowait(), b.queue.get_nowait()

    first, second = run(scenario())
    assert first["station_key"] == second["station_key"] == "N0CALL-7"


def test_events_are_isolated_from_each_other():
    async def scenario():
        hub = Hub()
        mine, theirs = hub.subscribe(1), hub.subscribe(2)
        await hub.publish(1, {"type": "position"})
        return mine.queue.qsize(), theirs.queue.qsize()

    assert run(scenario()) == (1, 0)


def test_a_stalled_subscriber_drops_messages_instead_of_blocking_ingest():
    """A phone that slept or hit a dead zone must not stall the feed.

    Dropping is safe: the client resyncs full state on reconnect, and the next
    position report supersedes the lost one anyway.
    """
    async def scenario():
        hub = Hub()
        stalled = hub.subscribe(1)
        healthy = hub.subscribe(1)
        # Fill the stalled subscriber's queue past capacity.
        for i in range(QUEUE_MAXSIZE + 10):
            healthy.queue.get_nowait() if not healthy.queue.empty() else None
            await hub.publish(1, {"type": "position", "n": i})
        return stalled, healthy

    stalled, healthy = run(scenario())
    assert stalled.queue.qsize() == QUEUE_MAXSIZE
    assert stalled.dropped == 10
    assert healthy.queue.qsize() <= QUEUE_MAXSIZE   # never grows unbounded


def test_unsubscribe_stops_delivery():
    async def scenario():
        hub = Hub()
        sub = hub.subscribe(1)
        hub.unsubscribe(sub)
        await hub.publish(1, {"type": "position"})
        return hub.subscriber_count(1), sub.queue.qsize()

    assert run(scenario()) == (0, 0)


def test_position_message_keeps_metric_units_on_the_wire():
    report = parse_packet(PACKET)
    message = position_message(report)
    assert message["speed_kmh"] == report.speed_kmh
    assert "speed_mph" not in message


def test_position_message_carries_roster_label_when_known():
    report = parse_packet(PACKET)
    roster_row = {"display_label": "Half-back", "category": "sweep"}
    message = position_message(report, roster_row)
    assert message["label"] == "Half-back"
    assert message["category"] == "sweep"


def test_position_message_without_roster_omits_label():
    """A station can report before the roster is filled in."""
    message = position_message(parse_packet(PACKET))
    assert "label" not in message
