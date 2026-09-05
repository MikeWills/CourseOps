"""A station the roster does not name must reach NCS without a refresh.

The SSID alerts are computed from stored positions and travel with the state
snapshot. A marker for the unexpected station arrived live; the alert about it
did not, until somebody reloaded - found on the first real feed, on race-week
check-in. The fix: the first packet from an unknown station triggers one
resync, which carries the alerts.
"""

import asyncio

from courseops import db, parser, progress, web


class FakeHub:
    def __init__(self):
        self.messages = []

    async def publish(self, event_id, message):
        self.messages.append((event_id, message))


def _report(key):
    return parser.PositionReport(
        station_key=key, received_at="2026-10-17T14:05:00Z",
        lat=44.16, lon=-94.0, course_deg=None, speed_kmh=None,
        altitude_m=None, symbol_table="/", symbol_code=">",
        comment=None, aprs_format=None, raw="",
    )


def _handler(tmp_path, hub, known):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    index = progress.CourseIndex.for_event(conn, event_id)
    roster_by_key = {"N0CALL-7": {"display_label": "Sweep", "category": "sweep"}}
    return web.make_position_handler(hub, roster_by_key, known, index), event_id


def _types(hub):
    return [m["type"] for _, m in hub.messages]


def test_a_rostered_station_is_just_a_position(tmp_path):
    hub = FakeHub()
    on_position, event_id = _handler(tmp_path, hub, known={"N0CALL-7"})
    asyncio.run(on_position(event_id, _report("N0CALL-7")))
    assert _types(hub) == ["position"]


def test_an_unknown_station_announces_itself_once(tmp_path):
    """One resync on the first packet - which carries the alert to every
    browser - and never again for that station, or an igate beaconing every
    thirty seconds would have every phone reloading all day."""
    hub = FakeHub()
    on_position, event_id = _handler(tmp_path, hub, known={"N0CALL-7"})
    asyncio.run(on_position(event_id, _report("N0CALL-5")))
    asyncio.run(on_position(event_id, _report("N0CALL-5")))
    asyncio.run(on_position(event_id, _report("N0CALL-5")))
    assert _types(hub) == ["position", "resync", "position", "position"]


def test_each_new_station_announces_separately(tmp_path):
    hub = FakeHub()
    on_position, event_id = _handler(tmp_path, hub, known={"N0CALL-7"})
    asyncio.run(on_position(event_id, _report("N0CALL-5")))
    asyncio.run(on_position(event_id, _report("W1AW-9")))
    assert _types(hub).count("resync") == 2


def test_a_bound_ssid_is_known(tmp_path):
    """A bare roster entry bound to the SSID it was heard on has already been
    attributed; hearing it again is not news."""
    hub = FakeHub()
    on_position, event_id = _handler(tmp_path, hub, known={"N0CALL", "N0CALL-9"})
    asyncio.run(on_position(event_id, _report("N0CALL-9")))
    assert _types(hub) == ["position"]
