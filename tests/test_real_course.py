"""Regression tests against a real organizer-grade course file.

`mankato_marathon.kml` is a genuine MapMyRun export of the 2026 Mankato
Marathon course, kept because synthetic fixtures do not reproduce what real
files do. It has already caught two bugs that hand-written fixtures missed:
identically-named placemarks distinguished only by `<styleUrl>`, and hint
patterns that could not match inside `start_marker`.

It is also the only realistic course available for Phase 5 (course-relative
position), so the measurements asserted here are the baseline that work should
be checked against.

NOTE before the repository is made public: this is the organizer's course data,
obtained from a publicly shared MapMyRun route. Decide then whether to keep it,
replace it with a synthetic equivalent, or seek permission.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aprswebtracker import db, geo, importer, kml

FIXTURE = Path(__file__).parent / "fixtures" / "mankato_marathon.kml"

# A marathon is 26.2188 mi. This export measures slightly long because the
# route was drawn by hand and cuts corners in places; see the gap test below.
OFFICIAL_MARATHON_MILES = 26.2188


@pytest.fixture
def features():
    return kml.load(FIXTURE)


def line(features):
    return next(f for f in features if f.geom_type == "linestring")


def test_the_whole_file_parses(features):
    """One route line plus start and finish markers."""
    assert len(features) == 3
    assert sum(1 for f in features if f.geom_type == "linestring") == 1
    assert sum(1 for f in features if f.geom_type == "point") == 2


def test_start_and_finish_are_distinguishable(features):
    """All three placemarks share the route's name; only styleUrl separates them.

    This is the case that broke the importer before styleUrl was captured.
    """
    points = [f for f in features if f.geom_type == "point"]
    assert len({f.name for f in points}) == 1          # identical names
    assert [f.suggest() for f in points] == ["poi:start", "poi:finish"]


def test_measured_distance_is_within_one_percent_of_a_marathon(features):
    miles = line(features).length_m / 1609.344
    assert abs(miles - OFFICIAL_MARATHON_MILES) / OFFICIAL_MARATHON_MILES < 0.01


def test_dedupe_removes_the_segment_join_duplicates(features):
    """MapMyRun repeats the vertex wherever two routing segments meet."""
    raw = line(features).coords
    cleaned = geo.dedupe_consecutive(raw)
    assert len(raw) == 1415
    assert len(cleaned) == 1258          # 157 duplicates removed


def test_the_route_is_point_to_point_not_a_loop(features):
    """Start and finish are ~2.5 km apart, which sets the default map view."""
    coords = geo.dedupe_consecutive(line(features).coords)
    assert geo.haversine_m(coords[0], coords[-1]) > 2000


def test_known_straight_line_gaps_are_documented_not_silently_accepted(features):
    """The route builder used direct/offroad mode in places, so the line cuts
    corners rather than following the road.

    Phase 5 must account for this: a chord across a corner is shorter than the
    road, so "mile 14.2" computed against this geometry drifts. A GIS-sourced
    course should not have these; assert the shape of the problem so a future
    change that silently smooths or splits the line is noticed.
    """
    coords = geo.dedupe_consecutive(line(features).coords)
    gaps = [geo.haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    big = [g for g in gaps if g > 200]
    assert len(big) == 13
    assert max(gaps) == pytest.approx(1241, abs=5)


def test_the_export_contains_no_aid_stations(features):
    """Aid station locations must come from elsewhere regardless of file format.

    Worth asserting: if a future export DOES carry them, this test fails and
    tells us the manual-entry step can be dropped.
    """
    assert not any(f.suggest() == "poi:aid_station" for f in features)


def test_end_to_end_import_of_the_real_file(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "mankato", "2026 Mankato Marathon")

    summary = importer.stage_file(conn, event_id, FIXTURE)
    assert summary.total == 3

    staged = importer.pending_features(conn, event_id)
    line_id = next(r["id"] for r in staged if r["geom_type"] == "linestring")
    point_ids = [r["id"] for r in staged if r["geom_type"] == "point"]

    course_id, distance_m, warnings = importer.assign_course(
        conn, event_id, [line_id], name="Full"
    )
    assert warnings == []           # single segment, nothing to stitch
    assert 26.0 < distance_m / 1609.344 < 26.6

    importer.assign_poi(conn, event_id, point_ids[0], "start", name="Start")
    importer.assign_poi(conn, event_id, point_ids[1], "finish", name="Finish")

    assert len(importer.courses_for_event(conn, event_id)) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM poi WHERE event_id = ?", (event_id,)
    ).fetchone()[0] == 2
