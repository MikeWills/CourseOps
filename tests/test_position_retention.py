"""One position per station, and nothing the map does not draw (#166).

The app only ever reads the NEWEST position of each station: the marker,
the snapshot, the SSID alerts. A row per fix was a record of where every
volunteer - and every phone-tracked medic - had been all day, with no
consumer. So a stored position REPLACES the station's previous one, and the
raw payload (an OwnTracks fix carries the phone's wifi SSID and BSSID) is
never written at all.
"""
from __future__ import annotations

import pytest

from courseops import db, tracker
from courseops.parser import PositionReport, parse_packet


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    return conn, event_id


def _report(key: str, at: str, lat: float = 44.1) -> PositionReport:
    return PositionReport(
        station_key=key, received_at=at, lat=lat, lon=-94.0,
        course_deg=None, speed_kmh=None, altitude_m=None,
        symbol_table="/", symbol_code="[", comment="hi", aprs_format="uncompressed",
        raw=f"{key}>APRS:!4406.00N/09400.00W[hi at {at}",
    )


def _rows(conn, event_id):
    return conn.execute(
        "SELECT station_key, received_at, lat, raw FROM position WHERE event_id = ?"
        " ORDER BY id", (event_id,)).fetchall()


def test_a_new_position_replaces_the_stations_previous_one(event):
    conn, event_id = event
    db.insert_position(conn, event_id, _report("N0CALL-7", "2026-10-17T14:00:00Z", 44.10))
    db.insert_position(conn, event_id, _report("N0CALL-7", "2026-10-17T14:05:00Z", 44.11))
    db.insert_position(conn, event_id, _report("W1AW-9", "2026-10-17T14:06:00Z", 44.20))
    rows = _rows(conn, event_id)
    assert [(r["station_key"], r["lat"]) for r in rows] == [("N0CALL-7", 44.11), ("W1AW-9", 44.20)]
    counts = conn.execute(
        "SELECT station_key, packets FROM position ORDER BY id").fetchall()
    assert [tuple(r) for r in counts] == [("N0CALL-7", 2), ("W1AW-9", 1)]


def test_an_older_fix_arriving_late_does_not_replace_a_newer_one(event):
    """A phone delivering its dead-zone backlog sends fixes in whatever
    order it kept them. The newest by REPORTED time is the one kept."""
    conn, event_id = event
    db.insert_position(conn, event_id, _report("M1", "2026-10-17T14:10:00Z", 44.10))
    db.insert_position(conn, event_id, _report("M1", "2026-10-17T14:02:00Z", 44.02))
    rows = _rows(conn, event_id)
    assert [(r["received_at"], r["lat"]) for r in rows] == [("2026-10-17T14:10:00Z", 44.10)]
    latest = db.latest_position_per_station(conn, event_id)
    assert [r["lat"] for r in latest] == [44.10]


def test_the_raw_payload_is_never_stored(event):
    conn, event_id = event
    db.insert_position(conn, event_id, _report("N0CALL-7", "2026-10-17T14:00:00Z"))
    assert _rows(conn, event_id)[0]["raw"] == ""


def test_a_phone_fix_stores_no_raw_either(event):
    conn, event_id = event
    designator, report = tracker.parse_osmand(
        {"id": "M1", "lat": "44.1", "lon": "-94.0", "timestamp": "1760709600"},
        raw="id=M1&lat=44.1&lon=-94.0&timestamp=1760709600&SSID=HomeWifi")
    tracker.store(conn, event_id, report)
    row = _rows(conn, event_id)[0]
    assert row["raw"] == ""


def test_an_existing_database_is_pruned_at_startup(tmp_path):
    """Databases from before this rule hold a row per fix and a raw line
    on each. `init_schema` runs on every start; it takes both away."""
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    for at in ("2026-10-17T14:00:00Z", "2026-10-17T14:05:00Z", "2026-10-17T14:03:00Z"):
        conn.execute(
            "INSERT INTO position (event_id, station_key, received_at, lat, lon, raw)"
            " VALUES (?, 'N0CALL-7', ?, 44.1, -94.0, 'the packet')", (event_id, at))
    conn.execute(
        "INSERT INTO position (event_id, station_key, received_at, lat, lon, raw)"
        " VALUES (?, 'W1AW-9', '2026-10-17T13:00:00Z', 44.2, -94.0, 'another')",
        (event_id,))
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 4

    db.init_schema(conn)
    rows = conn.execute(
        "SELECT station_key, received_at, raw, packets FROM position ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("N0CALL-7", "2026-10-17T14:05:00Z", "", 3),
        ("W1AW-9", "2026-10-17T13:00:00Z", "", 1),
    ]


def test_no_source_file_reads_the_raw_column():
    """Nothing draws it, so nothing may start depending on it."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "src" / "courseops"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert 'row["raw"]' not in text and "r['raw']" not in text, path
