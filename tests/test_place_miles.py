"""A place's mile is measured on a race it serves, not the nearest line.

The Mankato Full, Half and 10K share road for miles, and each place was
measured on whichever line happened to be a few metres nearer: the NCS
panel read WE "mile 2.4" (Half), WF "4.6" (10K) and WC "6.5" (Full), and
nothing a club could tick changed it. The races a place serves are already
stated (`poi_course`, the checkboxes on the Places tab); the mile is now
measured on one of them - the first in the Courses tab order, which the
club sets by dragging.

The same flaw sat under the lead runners, where it mattered more: each
stop had ONE distance, from the nearest line, used for every race's pace
and ETA. A Half leader's pace between two stops could come from Full miles.
The leader's own race now measures every stop.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, geo, leaders, progress, users
from courseops.config import Settings
from courseops.web import create_app

# Two 3 km routes on the same road, 4 m apart. The Full is drawn south to
# north, the Half north to south, so their miles along it disagree.
SOUTH, NORTH = 44.10, 44.10 + 3000 / 111_195
FULL = [(-94.0, SOUTH + i * (NORTH - SOUTH) / 30) for i in range(31)]
HALF = [(-94.00005, NORTH - i * (NORTH - SOUTH) / 30) for i in range(31)]
LON_NEAR_HALF = -94.00006          # 1 m from the Half, 5 m from the Full


def _lat_at(metres_from_south):
    return SOUTH + metres_from_south / 111_195


def _course(conn, event_id, name, coords, sort_order):
    return conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m, sort_order)"
        " VALUES (?, ?, ?, ?, ?)",
        (event_id, name, json.dumps(geo.to_geojson_linestring(coords)),
         geo.line_length_m(coords), sort_order)).lastrowid


def _place(conn, event_id, name, metres_from_south, serves, order):
    poi_id = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order)"
        " VALUES (?, ?, 'aid_station', ?, ?, ?)",
        (event_id, name, _lat_at(metres_from_south), LON_NEAR_HALF, order)
    ).lastrowid
    for course_id in serves:
        conn.execute("INSERT INTO poi_course (event_id, poi_id, course_id)"
                     " VALUES (?, ?, ?)", (event_id, poi_id, course_id))
    return poi_id


@pytest.fixture
def event(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    # Full on top of the Courses tab (highest sort_order), as at Mankato.
    full = _course(conn, event_id, "Full", FULL, 30)
    half = _course(conn, event_id, "Half", HALF, 20)
    return conn, db_path, event_id, full, half


def _position(conn, event_id, poi_id):
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = index.order_along_course(conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)).fetchall())
    return index.place_positions(
        rows, progress.served_courses(conn, event_id))[poi_id]


def test_nothing_ticked_keeps_the_nearest_line(event):
    conn, _, event_id, _, _ = event
    poi = _place(conn, event_id, "A", 1000, [], 1)
    pos = _position(conn, event_id, poi)
    assert pos.course_name == "Half"
    assert pos.distance_along_m == pytest.approx(2000, abs=5)


def test_one_race_ticked_measures_on_that_race(event):
    """Nearer the Half's line, but it serves the Full: Full mile."""
    conn, _, event_id, full, _ = event
    poi = _place(conn, event_id, "A", 1000, [full], 1)
    pos = _position(conn, event_id, poi)
    assert pos.course_name == "Full"
    assert pos.distance_along_m == pytest.approx(1000, abs=5)


def test_several_ticked_measures_on_the_top_of_the_courses_tab(event):
    conn, _, event_id, full, half = event
    poi = _place(conn, event_id, "A", 1000, [half, full], 1)
    assert _position(conn, event_id, poi).course_name == "Full"
    # Drag the Half to the top and it is the Half's mile.
    conn.execute("UPDATE course SET sort_order = 40 WHERE id = ?", (half,))
    assert _position(conn, event_id, poi).course_name == "Half"


def test_a_ticked_race_far_from_the_place_falls_back_to_the_nearest(event):
    """A tick for a race 2 km away is a wrong tick; the mile stays what it
    was before rather than becoming none."""
    conn, _, event_id, _, _ = event
    far = _course(conn, event_id, "10K",
                  [(-93.97, SOUTH), (-93.97, NORTH)], 10)
    poi = _place(conn, event_id, "A", 1000, [far], 1)
    assert _position(conn, event_id, poi).course_name == "Half"


def test_the_snapshot_and_a_posted_station_carry_the_served_race_mile(event):
    conn, db_path, event_id, full, _ = event
    poi = _place(conn, event_id, "A", 1000, [full], 1)
    db.upsert_roster_entry(conn, event_id, "KD0AAA", "WA", "aid_station",
                           expects_aprs=False)
    db.assign_station_to_poi(conn, event_id, "KD0AAA", poi)
    tokens = access.ensure_tokens(conn, event_id)
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    with TestClient(app) as client:
        state = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    place = next(p for p in state["pois"] if p["id"] == poi)
    station = next(r for r in state["roster"] if r["station_key"] == "KD0AAA")
    for where in (place, station):
        assert where["course_position"]["course_name"] == "Full"
        assert where["course_position"]["distance_along_m"] == pytest.approx(1000, abs=5)


def test_the_places_tab_shows_the_served_race_mile(event):
    conn, _, event_id, full, _ = event
    poi = _place(conn, event_id, "A", 1000, [full], 1)
    row = next(p for p in admin.list_pois(conn, event_id) if p["id"] == poi)
    assert row["course_name"] == "Full"
    assert row["distance_along_m"] == pytest.approx(1000, abs=5)


def test_a_half_leaders_pace_is_measured_on_the_half(event):
    """Both stops sit nearer the Half's line but serve both races, and the
    Half runs north to south. Measured on the Full (the old single distance
    per stop, for a stop nearest the Full) the leg ran backwards and the
    pace came out as nothing; on the Half it is 1000 m in 5 minutes."""
    conn, _, event_id, full, half = event
    # Nearer the FULL this time, so the old single distance was the Full's.
    near_full = -93.99999
    p_north = _place(conn, event_id, "N", 2000, [full, half], 1)
    p_south = _place(conn, event_id, "S", 1000, [full, half], 2)
    conn.execute("UPDATE poi SET lon = ? WHERE event_id = ?", (near_full, event_id))
    leaders.record_sighting(conn, event_id, half, "male", p_north)
    leaders.record_sighting(conn, event_id, half, "male", p_south)
    conn.execute(
        "UPDATE lead_sighting SET at = CASE poi_id WHEN ? THEN"
        " '2026-10-17T13:00:00Z' ELSE '2026-10-17T13:05:00Z' END",
        (p_north,))
    index = progress.CourseIndex.for_event(conn, event_id)
    entry = next(l for l in leaders.for_event(conn, event_id, index)
                 if l.course_id == half and l.division == "male")
    assert entry.last_distance_m == pytest.approx(2000, abs=5)
    assert entry.pace_mps == pytest.approx(1000 / 300, rel=0.01)


# --- a stop passed more than once ----------------------------------------------
#
# The Mankato Full passes water stop I at mile 16.3 and again at 20.6, and
# the nearest pass (54 m against 80) was the later one - so I read 20.6,
# after J at 16.8, and a Full leader's I -> J leg ran backwards: no pace, no
# ETA. The club's order settles it: one pass per stop, never running
# backwards in that order, and of those the nearest to the line in total.

# An out-and-back: 3 km north, then back south on a road 56 m east. A point
# m metres north is at m on the way out and at 6056 - m on the way back.
NEARER_OUT, NEARER_BACK = -93.99990, -93.99940     # 8 m from one, 48 from the other


def _out_and_back():
    out = [(-94.0, _lat_at(i * 100)) for i in range(31)]
    back = [(-93.9993, _lat_at(3000 - i * 100)) for i in range(31)]
    return out + back


def _stop(conn, event_id, name, metres_north, lon, serves, order):
    poi_id = _place(conn, event_id, name, metres_north, serves, order)
    conn.execute("UPDATE poi SET lon = ? WHERE id = ?", (lon, poi_id))
    return poi_id


@pytest.fixture
def out_and_back(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    course = _course(conn, event_id, "Full", _out_and_back(), 30)
    stops = {
        # The Mankato shape: I sits nearer its LATER pass (4056), and taking
        # it leaves J nowhere to go but backwards.
        "H": _stop(conn, event_id, "H", 1000, NEARER_OUT, [course], 1),
        "I": _stop(conn, event_id, "I", 2000, NEARER_BACK, [course], 2),
        "J": _stop(conn, event_id, "J", 2500, NEARER_OUT, [course], 3),
        # And the nearest pass is still taken where the order allows it: K is
        # on the way back, where it sits 8 m from the line.
        "K": _stop(conn, event_id, "K", 1500, NEARER_BACK, [course], 4),
    }
    return conn, event_id, course, stops


def _place_miles(conn, event_id):
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = index.order_along_course(conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)).fetchall())
    found = index.place_positions(rows, progress.served_courses(conn, event_id))
    return {pid: pos.distance_along_m for pid, pos in found.items()}


def test_the_pass_is_the_one_the_clubs_order_allows(out_and_back):
    conn, event_id, _, stops = out_and_back
    miles = _place_miles(conn, event_id)
    assert miles[stops["H"]] == pytest.approx(1000, abs=10)
    assert miles[stops["I"]] == pytest.approx(2000, abs=10)     # not 4056
    assert miles[stops["J"]] == pytest.approx(2500, abs=10)
    assert miles[stops["K"]] == pytest.approx(4556, abs=10)     # nearest, in order


def test_the_places_tab_uses_the_same_pass(out_and_back):
    conn, event_id, _, stops = out_and_back
    row = next(p for p in admin.list_pois(conn, event_id) if p["id"] == stops["I"])
    assert row["distance_along_m"] == pytest.approx(2000, abs=10)


def test_a_leader_across_a_twice_passed_stop_has_a_pace(out_and_back):
    conn, event_id, course, stops = out_and_back
    leaders.record_sighting(conn, event_id, course, "male", stops["I"])
    leaders.record_sighting(conn, event_id, course, "male", stops["J"])
    conn.execute(
        "UPDATE lead_sighting SET at = CASE poi_id WHEN ? THEN"
        " '2026-10-17T13:00:00Z' ELSE '2026-10-17T13:03:00Z' END", (stops["I"],))
    index = progress.CourseIndex.for_event(conn, event_id)
    entry = next(l for l in leaders.for_event(conn, event_id, index)
                 if l.division == "male")
    assert entry.last_distance_m == pytest.approx(2500, abs=10)
    assert entry.pace_mps == pytest.approx(500 / 180, rel=0.02)
    assert entry.next_poi_name == "K"


# The organizer spaced the Mankato lines apart ON PURPOSE so each race shows
# on the map: stop A sits 284 m from the Half's line. A tick is the club
# saying it is on that route, so a ticked stop gets a wider reach on that
# race; an unticked one keeps the strict limit.
def _place_east(conn, event_id, name, metres_from_south, metres_east, serves, order):
    lon = -94.00005 + metres_east / (111_195 * 0.7193)    # cos(44.1 deg)
    poi_id = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order)"
        " VALUES (?, ?, 'aid_station', ?, ?, ?)",
        (event_id, name, _lat_at(metres_from_south), lon, order)).lastrowid
    for course_id in serves:
        conn.execute("INSERT INTO poi_course (event_id, poi_id, course_id)"
                     " VALUES (?, ?, ?)", (event_id, poi_id, course_id))
    return poi_id


def test_a_ticked_stop_beyond_the_strict_limit_still_gets_its_races_mile(event):
    conn, _, event_id, _, half = event
    poi = _place_east(conn, event_id, "A", 1000, 284, [half], 1)
    pos = _position(conn, event_id, poi)
    assert pos.course_name == "Half"
    assert pos.distance_along_m == pytest.approx(2000, abs=5)
    assert pos.offset_m == pytest.approx(284, abs=10)


def test_an_unticked_stop_that_far_still_has_no_mile(event):
    conn, _, event_id, _, _ = event
    poi = _place_east(conn, event_id, "A", 1000, 284, [], 1)
    assert _position(conn, event_id, poi) is None


def test_even_a_ticked_stop_has_a_limit(event):
    conn, _, event_id, _, half = event
    poi = _place_east(conn, event_id, "A", 1000, 900, [half], 1)
    assert _position(conn, event_id, poi) is None


def test_the_leader_progression_measures_a_ticked_far_stop(event):
    conn, _, event_id, _, half = event
    near = _place(conn, event_id, "B", 500, [half], 2)
    far = _place_east(conn, event_id, "A", 1000, 284, [half], 1)
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = conn.execute("SELECT * FROM poi WHERE event_id = ? ORDER BY sort_order",
                        (event_id,)).fetchall()
    along = index.progression(half, rows, stated={near, far})
    assert along[far] is not None
    assert along[far].distance_along_m < along[near].distance_along_m
