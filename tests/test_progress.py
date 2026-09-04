"""Course-relative position: "Full-back at mile 14.2"."""

from __future__ import annotations

from pathlib import Path

import pytest

from courseops import db, geo, importer, progress

COURSE = Path(__file__).parent / "fixtures" / "consumer_export_course.kml"
MILE = 1609.344


@pytest.fixture
def course_file(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "course_file", "Example Marathon")
    importer.stage_file(conn, event_id, COURSE)
    line = next(r["id"] for r in importer.pending_features(conn, event_id)
                if r["geom_type"] == "linestring")
    importer.assign_course(conn, event_id, [line], name="Full")
    return conn, event_id


# --- projection geometry ----------------------------------------------------

def test_projection_along_a_straight_line():
    """A point beside the midpoint reports half the length, and its offset."""
    line = [(-93.99, 44.14), (-93.99, 44.16)]
    totals = geo.cumulative_lengths(line)
    midpoint_lat = 44.15

    projection = geo.project_onto_line(line, (-93.99, midpoint_lat), totals)

    assert projection.distance_along_m == pytest.approx(totals[-1] / 2, rel=1e-3)
    assert projection.offset_m == pytest.approx(0.0, abs=0.5)


def test_offset_is_perpendicular_distance():
    line = [(-93.99, 44.14), (-93.99, 44.16)]
    # ~0.001 degrees of longitude east at this latitude is about 80 m.
    projection = geo.project_onto_line(line, (-93.989, 44.15))
    assert 70 < projection.offset_m < 90


def test_projection_clamps_past_the_ends():
    """A point beyond the finish must report the finish, not an extrapolation."""
    line = [(-93.99, 44.14), (-93.99, 44.16)]
    totals = geo.cumulative_lengths(line)

    beyond = geo.project_onto_line(line, (-93.99, 44.20), totals)
    before = geo.project_onto_line(line, (-93.99, 44.10), totals)

    assert beyond.distance_along_m == pytest.approx(totals[-1], abs=1.0)
    assert before.distance_along_m == pytest.approx(0.0, abs=1.0)
    # The offset still reports how far away they actually are.
    assert beyond.offset_m > 4000


def test_degenerate_line_returns_none():
    assert geo.project_onto_line([(-93.99, 44.14)], (-93.99, 44.15)) is None


# --- against the real course ------------------------------------------------

def test_every_vertex_of_the_course_snaps_cleanly(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]

    for i in range(0, len(course.coords), 97):        # sample across the route
        located = index.locate(course.coords[i][1], course.coords[i][0])
        assert located is not None
        assert located.offset_m < 1.0
        assert located.distance_along_m == pytest.approx(course.totals[i], abs=1.0)


def test_start_and_finish_of_the_course(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]

    start = index.locate(course.coords[0][1], course.coords[0][0])
    finish = index.locate(course.coords[-1][1], course.coords[-1][0])

    assert start.distance_along_m == pytest.approx(0.0, abs=1.0)
    assert start.remaining_m / MILE == pytest.approx(26.27, abs=0.1)
    assert finish.remaining_m == pytest.approx(0.0, abs=1.0)
    assert finish.fraction == pytest.approx(1.0, abs=0.001)


def _off_course(index, metres: float):
    """A point `metres` north of the course's midpoint.

    Derived rather than hardcoded: a magic lat/lon is only "1.7 km off the
    route" for one particular route, and silently becomes something else the
    day the fixture is regenerated.
    """
    course = index._courses[0]
    lon, lat = course.coords[len(course.coords) // 2]
    return lat + metres / 111_320.0, lon


def test_a_station_far_from_the_course_gets_no_mile_figure(course_file):
    """Better no number than a plausible wrong one - someone will act on it."""
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    lat, lon = _off_course(index, 1_700)

    assert index.locate(lat, lon) is None


def test_tolerance_is_adjustable(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    lat, lon = _off_course(index, 1_700)

    lenient = progress.CourseIndex.for_event(conn, event_id, max_offset_m=3000)
    assert lenient.locate(lat, lon) is not None


def test_fraction_and_remaining_agree_with_length(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]
    midpoint = course.coords[len(course.coords) // 2]

    located = index.locate(midpoint[1], midpoint[0])

    assert located.distance_along_m + located.remaining_m == pytest.approx(
        located.course_length_m, abs=1.0
    )
    assert 0.0 < located.fraction < 1.0


# --- several courses --------------------------------------------------------

def test_the_nearest_course_wins(tmp_path):
    """Courses share road for miles, so the name always travels with the mile."""
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    importer.stage_file(conn, event_id, COURSE)
    lines = [r["id"] for r in importer.pending_features(conn, event_id)
             if r["geom_type"] == "linestring"]
    importer.assign_course(conn, event_id, [lines[0]], name="Full")

    # A second, quite different course some way to the east.
    conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m, sort_order)"
        " VALUES (?, 'Half', ?, 1000, 1)",
        (event_id,
         '{"type":"LineString","coordinates":[[-93.90,44.10],[-93.90,44.20]]}'),
    )

    index = progress.CourseIndex.for_event(conn, event_id)
    assert len(index) == 2

    on_half = index.locate(44.15, -93.9005)
    assert on_half.course_name == "Half"


def test_no_courses_means_no_position(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    index = progress.CourseIndex.for_event(conn, event_id)
    assert len(index) == 0
    assert index.locate(44.15, -93.99) is None


# --- ordering by course position --------------------------------------------
#
# Aid stations get named however a club likes: numbers, Greek letters, NATO
# phonetic, place names. None of those sort correctly by name, so they are
# ordered by where they sit on the course instead.

def aid_stations_at_miles(conn, event_id, index, plan):
    """Place named aid stations at real points along the course."""
    course = index._courses[0]

    def at_mile(mile):
        target = mile * MILE
        for i, total in enumerate(course.totals):
            if total >= target:
                return course.coords[i]
        return course.coords[-1]

    for name, mile in plan:
        lon, lat = at_mile(mile)
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', ?, ?)",
            (event_id, name, lat, lon),
        )
    return conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()


def test_greek_letters_sort_wrong_by_name_but_right_by_course(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = aid_stations_at_miles(conn, event_id, index, [
        ("Alpha", 2.5), ("Beta", 5.0), ("Gamma", 8.5),
        ("Delta", 12.0), ("Epsilon", 17.0),
    ])

    by_name = [r["name"] for r in sorted(rows, key=lambda r: r["name"])]
    by_course = [r["name"] for r in index.order_along_course(rows)]

    # The bug this exists to avoid: Gamma is third on the course, fifth by name.
    assert by_name == ["Alpha", "Beta", "Delta", "Epsilon", "Gamma"]
    assert by_course == ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]


def test_numbered_stations_sort_wrong_by_name_but_right_by_course(course_file):
    """'Aid 10' sorts before 'Aid 2' as a string."""
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = aid_stations_at_miles(conn, event_id, index, [
        ("Aid 2", 4.0), ("Aid 10", 20.0),
    ])

    assert [r["name"] for r in sorted(rows, key=lambda r: r["name"])] == \
        ["Aid 10", "Aid 2"]
    assert [r["name"] for r in index.order_along_course(rows)] == \
        ["Aid 2", "Aid 10"]


def test_places_off_the_course_sink_to_the_end(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    aid_stations_at_miles(conn, event_id, index, [("Alpha", 2.5), ("Beta", 9.0)])
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'Overflow Parking', 'parking', 44.30, -93.60)",
        (event_id,),
    )
    rows = conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()

    ordered = [r["name"] for r in index.order_along_course(rows)]

    assert ordered == ["Alpha", "Beta", "Overflow Parking"]


def test_posting_a_station_at_an_aid_station_gives_it_a_position(course_file):
    """Most aid station operators never beacon, so their only position is the
    station they are posted at."""
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = aid_stations_at_miles(conn, event_id, index, [("Alpha", 6.0)])
    db.upsert_roster_entry(conn, event_id, "N0AAA-1", "Aid Alpha",
                           "aid_station", expects_aprs=False)

    updated = db.assign_station_to_poi(conn, event_id, "N0AAA-1", rows[0]["id"])

    assert updated["poi_id"] == rows[0]["id"]
    located = index.locate(rows[0]["lat"], rows[0]["lon"])
    assert located.distance_along_m / MILE == pytest.approx(6.0, abs=0.1)


def test_posting_to_an_unknown_poi_is_rejected(course_file):
    conn, event_id = course_file
    db.upsert_roster_entry(conn, event_id, "N0AAA-1", "Aid Alpha", "aid_station")
    with pytest.raises(ValueError, match="No POI with id"):
        db.assign_station_to_poi(conn, event_id, "N0AAA-1", 9999)


def test_posting_can_be_cleared(course_file):
    conn, event_id = course_file
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = aid_stations_at_miles(conn, event_id, index, [("Alpha", 6.0)])
    db.upsert_roster_entry(conn, event_id, "N0AAA-1", "Aid Alpha", "aid_station")

    db.assign_station_to_poi(conn, event_id, "N0AAA-1", rows[0]["id"])
    cleared = db.assign_station_to_poi(conn, event_id, "N0AAA-1", None)

    assert cleared["poi_id"] is None
