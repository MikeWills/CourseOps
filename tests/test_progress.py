"""Course-relative position: "Full-back at mile 14.2"."""

from __future__ import annotations

from pathlib import Path

import pytest

from courseops import db, geo, importer, progress

REAL = Path(__file__).parent / "fixtures" / "mankato_marathon.kml"
MILE = 1609.344


@pytest.fixture
def mankato(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "mankato", "Mankato")
    importer.stage_file(conn, event_id, REAL)
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

def test_every_vertex_of_the_real_course_snaps_cleanly(mankato):
    conn, event_id = mankato
    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]

    for i in range(0, len(course.coords), 97):        # sample across the route
        located = index.locate(course.coords[i][1], course.coords[i][0])
        assert located is not None
        assert located.offset_m < 1.0
        assert located.distance_along_m == pytest.approx(course.totals[i], abs=1.0)


def test_start_and_finish_of_the_real_course(mankato):
    conn, event_id = mankato
    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]

    start = index.locate(course.coords[0][1], course.coords[0][0])
    finish = index.locate(course.coords[-1][1], course.coords[-1][0])

    assert start.distance_along_m == pytest.approx(0.0, abs=1.0)
    assert start.remaining_m / MILE == pytest.approx(26.4, abs=0.1)
    assert finish.remaining_m == pytest.approx(0.0, abs=1.0)
    assert finish.fraction == pytest.approx(1.0, abs=0.001)


def test_a_station_far_from_the_course_gets_no_mile_figure(mankato):
    """Better no number than a plausible wrong one - someone will act on it."""
    conn, event_id = mankato
    index = progress.CourseIndex.for_event(conn, event_id)

    assert index.locate(44.16, -93.95) is None       # ~1.7 km off the route


def test_tolerance_is_adjustable(mankato):
    conn, event_id = mankato
    lenient = progress.CourseIndex.for_event(conn, event_id, max_offset_m=3000)
    assert lenient.locate(44.16, -93.95) is not None


def test_fraction_and_remaining_agree_with_length(mankato):
    conn, event_id = mankato
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
    importer.stage_file(conn, event_id, REAL)
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
