"""Two-phase import: stage for review, then commit assignments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from courseops import db, importer, units

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def event_db(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon 2026")
    return conn, event_id


def staged(conn, event_id, fragment):
    for row in importer.pending_features(conn, event_id, include_all=True):
        if fragment.lower() in row["name"].lower():
            return row
    raise AssertionError(f"no staged feature matching {fragment!r}")


def test_staging_does_not_create_courses_or_pois(event_db):
    """Nothing reaches course/poi without a human confirming it."""
    conn, event_id = event_db
    summary = importer.stage_file(conn, event_id, FIXTURE)

    assert summary.total == 11
    assert conn.execute("SELECT COUNT(*) FROM course").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 0
    assert len(importer.pending_features(conn, event_id)) == 11


def test_import_is_additive_across_files(event_db):
    """The full course, half course and water stops arrive as separate files."""
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    importer.stage_file(conn, event_id, FIXTURE)

    assert len(importer.pending_features(conn, event_id)) == 22
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 2


def test_assign_course_stitches_segments_including_a_reversed_one(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    part1 = staged(conn, event_id, "Half Marathon - Part 1")
    part2 = staged(conn, event_id, "Half Marathon - Part 2")

    course_id, distance_m, warnings = importer.assign_course(
        conn, event_id, [part1["id"], part2["id"]], name="Half", color="#c33"
    )

    assert warnings == []  # the segments meet, so no gap is reported
    row = conn.execute("SELECT * FROM course WHERE id = ?", (course_id,)).fetchone()
    assert row["name"] == "Half"
    assert row["distance_m"] == pytest.approx(distance_m)
    coords = json.loads(row["geojson"])["coordinates"]
    # Part 2 was drawn backwards; stitching must have flipped it so the course
    # runs continuously from the start line to the far end.
    assert coords[0] == [-86.58, 34.73]
    assert coords[-1] == [-86.56, 34.741]


def test_assigned_features_leave_the_pending_queue(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    part1 = staged(conn, event_id, "Half Marathon - Part 1")

    importer.assign_course(conn, event_id, [part1["id"]], name="Half")

    pending_ids = {r["id"] for r in importer.pending_features(conn, event_id)}
    assert part1["id"] not in pending_ids
    assert staged(conn, event_id, "Part 1")["status"] == "assigned"


def test_assign_course_rejects_a_point(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    water = staged(conn, event_id, "Water Stop 1")

    with pytest.raises(ValueError, match="points, not lines"):
        importer.assign_course(conn, event_id, [water["id"]], name="Nope")


def test_assign_course_reverse_flag(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    part1 = staged(conn, event_id, "Half Marathon - Part 1")

    course_id, _, _ = importer.assign_course(
        conn, event_id, [part1["id"]], name="Backwards", reverse=True
    )
    coords = json.loads(
        conn.execute("SELECT geojson FROM course WHERE id = ?", (course_id,))
        .fetchone()["geojson"]
    )["coordinates"]
    assert coords[0] == [-86.57, 34.735]


def test_assign_poi_carries_description_and_w3w(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    water = staged(conn, event_id, "Water Stop 1")

    poi_id = importer.assign_poi(
        conn, event_id, water["id"], "aid_station", what3words="filled.count.soap"
    )

    row = conn.execute("SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone()
    assert row["name"] == "Water Stop 1"
    assert row["poi_type"] == "aid_station"
    assert row["notes"] == "Corner of Oak and 3rd"
    assert row["what3words"] == "filled.count.soap"
    assert (row["lon"], row["lat"]) == (-86.575, 34.732)


def test_discard_removes_from_review(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    junk = staged(conn, event_id, "Porta-John")

    importer.discard(conn, [junk["id"]])

    pending_ids = {r["id"] for r in importer.pending_features(conn, event_id)}
    assert junk["id"] not in pending_ids


def test_suggest_event_center_ignores_discarded(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)

    center = importer.suggest_event_center(conn, event_id)

    assert center is not None
    lon, lat = center
    assert -86.61 < lon < -86.55
    assert 34.72 < lat < 34.75


def test_course_distance_reads_in_miles(event_db):
    """The net speaks miles; storage stays metric."""
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    part1 = staged(conn, event_id, "Half Marathon - Part 1")

    _, distance_m, _ = importer.assign_course(
        conn, event_id, [part1["id"]], name="Half"
    )
    assert units.format_distance(distance_m).endswith(" mi")


def test_assign_poi_drops_an_exporter_attribute_table(event_db):
    """An ArcGIS description is an HTML document. It classified the place at
    staging; nothing in it belongs in a popup."""
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    water = staged(conn, event_id, "Water Stop 1")
    conn.execute(
        "UPDATE import_feature SET description = ? WHERE id = ?",
        ("<html><body><table><tr><td>Type</td><td>WATER</td></tr>"
         "</table></body></html>", water["id"]),
    )

    poi_id = importer.assign_poi(conn, event_id, water["id"], "aid_station")

    row = conn.execute("SELECT notes FROM poi WHERE id = ?", (poi_id,)).fetchone()
    assert row["notes"] is None


def test_existing_html_notes_are_cleaned_on_startup(event_db):
    """Events imported before the fix already carry the markup. Re-running
    init_schema - which every start does - strips it, and leaves alone notes
    that are plain text."""
    from courseops import db
    conn, event_id = event_db
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, notes)"
        " VALUES (?, 'Start', 'start', 44.1, -93.9, ?)",
        (event_id, "<html><body><table><tr><td>Type</td><td>Start</td></tr>"
                   "</table></body></html>"),
    )
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, notes)"
        " VALUES (?, 'Aid 2', 'aid_station', 44.2, -93.9, ?)",
        (event_id, "Behind the church <b>use the side gate</b>"),
    )
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, notes)"
        " VALUES (?, 'Aid 3', 'aid_station', 44.3, -93.9, 'Plain text')",
        (event_id,),
    )

    db.init_schema(conn)

    notes = dict(conn.execute(
        "SELECT name, notes FROM poi WHERE event_id = ?", (event_id,)).fetchall())
    assert notes == {"Start": None,
                     "Aid 2": "Behind the church use the side gate",
                     "Aid 3": "Plain text"}
