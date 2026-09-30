"""An updated file replaces a race's line or a place's position in place.

Import only ever added: an organizer's revised Half became a SECOND
"Half" with none of the first one's settings, and a moved water stop a
second "A" with no ticks, no order, no What3Words and nobody posted -
while deleting the originals was refused once anything referred to them.
Two weeks before a race, a revised route is normal. So a staged line can
REPLACE an existing course's geometry, and a staged point an existing
place's position: everything the club set stays, only where it is moves.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, geo, importer, leaders, users, web
from courseops.config import Settings
from courseops.web import create_app

STADIUM, DOWNTOWN = (-94.0, 44.162), (-94.0, 44.18)
# The original line, drawn downtown -> stadium, 2 km.
OLD = [(-94.0, 44.18 - i * 0.0018) for i in range(11)]
# The revision: the same ends, a 300 m detour east in the middle - and drawn
# backwards again, as the organizer draws it.
NEW = [(-94.0, 44.18), (-94.0, 44.175), (-93.996, 44.172), (-94.0, 44.169),
       (-94.0, 44.162)]


def _stage(conn, event_id, name, geometry):
    batch = conn.execute(
        "INSERT INTO import_batch (event_id, filename, source_kind)"
        " VALUES (?, 'rev.kml', 'kml')",
        (event_id,)).lastrowid
    return conn.execute(
        "INSERT INTO import_feature (batch_id, event_id, name, geom_type, geojson)"
        " VALUES (?, ?, ?, ?, ?)",
        (batch, event_id, name, geometry["type"].lower(), json.dumps(geometry))
    ).lastrowid


def _line(coords):
    return geo.to_geojson_linestring(coords)


def _point(lonlat):
    return {"type": "Point", "coordinates": list(lonlat)}


@pytest.fixture
def event(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    first = _stage(conn, event_id, "Half", _line(OLD))
    course_id, _, _ = importer.assign_course(conn, event_id, [first], name="Half")
    start = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon) VALUES (?, 'Stadium',"
        " 'start', ?, ?)", (event_id, STADIUM[1], STADIUM[0])).lastrowid
    water = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order, what3words)"
        " VALUES (?, 'A', 'aid_station', 44.171, -94.0, 3, 'filled.count.soap')",
        (event_id,)).lastrowid
    admin.update_course(conn, event_id, course_id, {
        "start_poi_id": start, "start_time": "07:30", "color": "#1a5fa5",
        "bib_color": "#6633cc", "bib_color_name": "Purple"})
    admin.set_poi_courses(conn, event_id, water, [course_id])
    db.upsert_roster_entry(conn, event_id, "KD0AAA", "WA", "aid_station",
                           expects_aprs=False)
    db.assign_station_to_poi(conn, event_id, "KD0AAA", water)
    leaders.record_sighting(conn, event_id, course_id, "male", water)
    return conn, db_path, event_id, course_id, water, first


def _course(conn, course_id):
    return conn.execute("SELECT * FROM course WHERE id = ?", (course_id,)).fetchone()


# --- a line -------------------------------------------------------------------

def test_a_new_line_replaces_the_geometry_and_keeps_everything_else(event):
    conn, _, event_id, course_id, water, _ = event
    before = dict(_course(conn, course_id))
    fid = _stage(conn, event_id, "Half v2", _line(NEW))
    result = admin.assign_features(conn, event_id, {
        "kind": "course", "ids": [fid], "replace_course_id": course_id})
    after = dict(_course(conn, course_id))

    assert result["course_id"] == course_id
    assert conn.execute("SELECT COUNT(*) FROM course").fetchone()[0] == 1
    for kept in ("name", "color", "bib_color", "bib_color_name", "start_time",
                 "start_poi_id", "sort_order"):
        assert after[kept] == before[kept], kept
    assert after["geojson"] != before["geojson"]
    assert after["distance_m"] == pytest.approx(result["distance_m"])
    # The ticks and the report of the leader passing A are the course's.
    assert conn.execute("SELECT COUNT(*) FROM poi_course WHERE course_id = ?",
                        (course_id,)).fetchone()[0] == 1
    assert len(leaders.sightings(conn, event_id, course_id, "male")) == 1


def test_the_new_line_is_turned_to_run_from_the_races_start(event):
    """The revision is drawn downtown-first like the original; the race's
    Starts at is still the stadium, so the new line is turned too."""
    conn, _, event_id, course_id, _, _ = event
    fid = _stage(conn, event_id, "Half v2", _line(NEW))
    admin.assign_features(conn, event_id, {
        "kind": "course", "ids": [fid], "replace_course_id": course_id})
    first = json.loads(_course(conn, course_id)["geojson"])["coordinates"][0]
    assert tuple(first) == pytest.approx(STADIUM)


def test_a_line_whose_ends_miss_the_start_is_refused_and_nothing_changes(event):
    conn, _, event_id, course_id, _, _ = event
    before = _course(conn, course_id)["geojson"]
    elsewhere = [(-93.9, 44.3), (-93.9, 44.32)]
    fid = _stage(conn, event_id, "Wrong file", _line(elsewhere))
    with pytest.raises(ValueError, match="Stadium"):
        admin.assign_features(conn, event_id, {
            "kind": "course", "ids": [fid], "replace_course_id": course_id})
    assert _course(conn, course_id)["geojson"] == before
    status = conn.execute("SELECT status FROM import_feature WHERE id = ?",
                          (fid,)).fetchone()["status"]
    assert status == "pending"


def test_the_replaced_features_leave_the_review_for_good(event):
    """The old line's staged features were `assigned` to this course; left
    so, deleting the course later would put BOTH versions back in review."""
    conn, _, event_id, course_id, _, first = event
    fid = _stage(conn, event_id, "Half v2", _line(NEW))
    admin.assign_features(conn, event_id, {
        "kind": "course", "ids": [fid], "replace_course_id": course_id})
    status = {r["id"]: (r["status"], r["course_id"]) for r in conn.execute(
        "SELECT id, status, course_id FROM import_feature")}
    assert status[fid] == ("assigned", course_id)
    assert status[first][0] == "discarded"


def test_a_course_from_another_event_cannot_be_replaced(event):
    conn, _, event_id, _, _, _ = event
    other = db.create_event(conn, "other", "Other")
    theirs, _, _ = importer.assign_course(
        conn, other, [_stage(conn, other, "Theirs", _line(OLD))], name="Theirs")
    fid = _stage(conn, event_id, "Half v2", _line(NEW))
    with pytest.raises(ValueError):
        admin.assign_features(conn, event_id, {
            "kind": "course", "ids": [fid], "replace_course_id": theirs})


# --- a stop -------------------------------------------------------------------

def test_a_point_moves_a_place_and_keeps_everything_else(event):
    conn, _, event_id, course_id, water, _ = event
    before = dict(conn.execute("SELECT * FROM poi WHERE id = ?", (water,)).fetchone())
    moved_to = (-94.0, 44.1719)                     # 100 m north
    fid = _stage(conn, event_id, "WATER A (ALL)", _point(moved_to))
    result = admin.assign_features(conn, event_id, {
        "kind": "poi", "ids": [fid], "replace_poi_id": water})
    after = dict(conn.execute("SELECT * FROM poi WHERE id = ?", (water,)).fetchone())

    assert result["poi_ids"] == [water]
    assert (after["lon"], after["lat"]) == pytest.approx(moved_to)
    for kept in ("name", "poi_type", "sort_order", "what3words", "label", "notes"):
        assert after[kept] == before[kept], kept
    assert conn.execute("SELECT poi_id FROM roster WHERE station_key = 'KD0AAA'"
                        ).fetchone()["poi_id"] == water
    assert conn.execute("SELECT COUNT(*) FROM poi_course WHERE poi_id = ?",
                        (water,)).fetchone()[0] == 1
    # How far it moved: its What3Words square is where it USED to be.
    assert result["moved_m"] == pytest.approx(100, abs=3)


def test_one_point_replaces_one_place(event):
    conn, _, event_id, _, water, _ = event
    a = _stage(conn, event_id, "A", _point((-94.0, 44.17)))
    b = _stage(conn, event_id, "B", _point((-94.0, 44.171)))
    with pytest.raises(ValueError):
        admin.assign_features(conn, event_id, {
            "kind": "poi", "ids": [a, b], "replace_poi_id": water})


def test_a_line_cannot_replace_a_place(event):
    conn, _, event_id, _, water, _ = event
    fid = _stage(conn, event_id, "Half v2", _line(NEW))
    with pytest.raises(ValueError):
        admin.assign_features(conn, event_id, {
            "kind": "poi", "ids": [fid], "replace_poi_id": water})


def test_setup_replaces_through_the_assign_route(event):
    conn, db_path, event_id, course_id, water, _ = event
    line = _stage(conn, event_id, "Half v2", _line(NEW))
    point = _stage(conn, event_id, "A", _point((-94.0, 44.1719)))
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    url = f"/api/setup/events/{event_id}/assign"
    with TestClient(app) as client:
        client.post("/api/setup/login",
                    json={"username": "mike", "password": "a-long-enough-password"})
        a = client.post(url, json={"kind": "course", "ids": [line],
                                   "replace_course_id": str(course_id)})
        b = client.post(url, json={"kind": "poi", "ids": [point],
                                   "replace_poi_id": water})
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert b.json()["moved_m"] == pytest.approx(100, abs=3)


# --- the client ---------------------------------------------------------------

SETUP_HTML = (web.STATIC_DIR / "setup.html").read_text(encoding="utf-8")
SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")


def test_the_review_screen_offers_a_replacement():
    assert 'id="assign-replace"' in SETUP_HTML
    go = SETUP_JS[SETUP_JS.index("$('assign-go').addEventListener"):]
    go = go[:go.index("\n});\n")]
    assert "replace_course_id" in go and "replace_poi_id" in go
