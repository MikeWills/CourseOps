"""Which end of a race is the start is stated by the club, never assumed.

A course line runs whichever way the organizer happened to draw it. At the
2026 Mankato Marathon every race starts at the stadium and finishes
downtown, and the Half and 10K files were drawn downtown-to-stadium: every
mile on those races was measured from the finish, so water stop F read
"10K mile 1.7" when it is the third stop, at 4.6. The Full came out right
only because its eight segments happened to stitch in that order.

So each race names its start and finish places, and the stored line is
turned to run from the start. Everything measured along it - miles, the
lead runner progression, "remaining" - follows from the one geometry.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, geo, progress, users, web
from courseops.config import Settings
from courseops.web import create_app

# A straight 2 km line drawn from DOWNTOWN (north) to the STADIUM (south).
DOWNTOWN = (-94.0, 44.18)
STADIUM = (-94.0, 44.162)
LINE = [(-94.0, 44.18 - i * 0.0018) for i in range(11)]
MIDWAY_NORTH = (-94.0, 44.1764)      # 400 m south of downtown


def _poi(conn, event_id, name, poi_type, lonlat):
    return conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon) VALUES (?, ?, ?, ?, ?)",
        (event_id, name, poi_type, lonlat[1], lonlat[0])).lastrowid


@pytest.fixture
def race(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    course_id = conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m)"
        " VALUES (?, '10K', ?, ?)",
        (event_id, json.dumps(geo.to_geojson_linestring(LINE)),
         geo.line_length_m(LINE))).lastrowid
    places = {
        "stadium": _poi(conn, event_id, "Stadium", "start", STADIUM),
        "downtown": _poi(conn, event_id, "Downtown", "finish", DOWNTOWN),
        "water": _poi(conn, event_id, "F", "aid_station", MIDWAY_NORTH),
        "far": _poi(conn, event_id, "Elsewhere", "other", (-93.9, 44.3)),
    }
    return conn, db_path, event_id, course_id, places


def _first_point(conn, course_id):
    row = conn.execute("SELECT geojson FROM course WHERE id = ?", (course_id,)).fetchone()
    return tuple(json.loads(row["geojson"])["coordinates"][0])


def _mile_of(conn, event_id, lonlat):
    index = progress.CourseIndex.for_event(conn, event_id)
    return index.locate(lonlat[1], lonlat[0]).distance_along_m


def test_naming_the_start_turns_a_backwards_line_round(race):
    conn, _, event_id, course_id, places = race
    assert _mile_of(conn, event_id, MIDWAY_NORTH) == pytest.approx(400, abs=15)
    admin.update_course(conn, event_id, course_id,
                        {"start_poi_id": places["stadium"]})
    assert _first_point(conn, course_id) == pytest.approx(STADIUM)
    # F is now measured from the stadium: 1600 m in, not 400.
    assert _mile_of(conn, event_id, MIDWAY_NORTH) == pytest.approx(1600, abs=15)


def test_naming_the_finish_alone_is_enough(race):
    conn, _, event_id, course_id, places = race
    admin.update_course(conn, event_id, course_id,
                        {"finish_poi_id": places["downtown"]})
    assert _first_point(conn, course_id) == pytest.approx(STADIUM)


def test_a_line_already_the_right_way_round_is_left_alone(race):
    conn, _, event_id, course_id, places = race
    admin.update_course(conn, event_id, course_id,
                        {"start_poi_id": places["downtown"]})
    assert _first_point(conn, course_id) == pytest.approx(DOWNTOWN)


def test_the_choice_is_stored_and_saving_it_twice_does_not_turn_it_back(race):
    conn, _, event_id, course_id, places = race
    for _ in range(2):
        saved = admin.update_course(conn, event_id, course_id,
                                    {"start_poi_id": places["stadium"],
                                     "finish_poi_id": places["downtown"]})
    assert (saved["start_poi_id"], saved["finish_poi_id"]) == (
        places["stadium"], places["downtown"])
    assert _first_point(conn, course_id) == pytest.approx(STADIUM)


def test_a_place_nowhere_near_either_end_is_refused(race):
    """Better refused than guessed: turning the line on a place 15 km away
    would put every mile on the race wrong with nothing on screen to say so."""
    conn, _, event_id, course_id, places = race
    with pytest.raises(ValueError, match="Elsewhere"):
        admin.update_course(conn, event_id, course_id,
                            {"start_poi_id": places["far"]})
    assert _first_point(conn, course_id) == pytest.approx(DOWNTOWN)
    row = conn.execute("SELECT start_poi_id FROM course WHERE id = ?",
                       (course_id,)).fetchone()
    assert row["start_poi_id"] is None


def test_a_start_and_finish_at_the_same_end_is_refused(race):
    """Both near one end is a loop or a slip; either way it says nothing
    about direction, and the club is told rather than the line guessed at."""
    conn, _, event_id, course_id, places = race
    with pytest.raises(ValueError):
        admin.update_course(conn, event_id, course_id,
                            {"start_poi_id": places["stadium"],
                             "finish_poi_id": places["stadium"]})


def test_clearing_the_start_leaves_the_line_as_it_now_runs(race):
    conn, _, event_id, course_id, places = race
    admin.update_course(conn, event_id, course_id,
                        {"start_poi_id": places["stadium"]})
    saved = admin.update_course(conn, event_id, course_id, {"start_poi_id": ""})
    assert saved["start_poi_id"] is None
    assert _first_point(conn, course_id) == pytest.approx(STADIUM)


def test_a_place_from_another_event_is_refused(race):
    conn, _, event_id, course_id, _ = race
    other = db.create_event(conn, "other", "Other")
    foreign = _poi(conn, other, "Theirs", "start", STADIUM)
    with pytest.raises(ValueError):
        admin.update_course(conn, event_id, course_id, {"start_poi_id": foreign})


def test_deleting_the_start_place_clears_the_choice_not_the_course(race):
    conn, _, event_id, course_id, places = race
    admin.update_course(conn, event_id, course_id,
                        {"start_poi_id": places["stadium"]})
    conn.execute("DELETE FROM poi WHERE id = ?", (places["stadium"],))
    row = conn.execute("SELECT start_poi_id FROM course WHERE id = ?",
                       (course_id,)).fetchone()
    assert row["start_poi_id"] is None


def test_an_existing_database_gets_the_columns(tmp_path):
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    conn.execute("ALTER TABLE course DROP COLUMN start_poi_id")
    conn.execute("ALTER TABLE course DROP COLUMN finish_poi_id")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute("PRAGMA table_info(course)")}
    assert {"start_poi_id", "finish_poi_id"} <= names


def test_setup_saves_it_and_the_field_sees_the_new_miles(race):
    conn, db_path, event_id, course_id, places = race
    tokens = access.ensure_tokens(conn, event_id)
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    with TestClient(app) as client:
        client.post("/api/setup/login",
                    json={"username": "mike", "password": "a-long-enough-password"})
        bad = client.post(f"/api/setup/events/{event_id}/courses/{course_id}",
                          json={"start_poi_id": places["far"]})
        good = client.post(f"/api/setup/events/{event_id}/courses/{course_id}",
                           json={"start_poi_id": str(places["stadium"])})
        state = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert bad.status_code == 400 and "Elsewhere" in bad.json()["detail"]
    assert good.status_code == 200, good.text
    water = next(p for p in state["pois"] if p["name"] == "F")
    assert water["course_position"]["distance_along_m"] == pytest.approx(1600, abs=15)


# --- the client ---------------------------------------------------------------

SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")


def test_the_courses_table_picks_a_start_and_a_finish():
    table = SETUP_JS[SETUP_JS.index("$('course-table').innerHTML"):]
    table = table[:table.index("noun: 'course(s)'")]
    assert "data-startpoi=" in table and "data-finishpoi=" in table
    assert "{ attr: 'startpoi', name: 'start_poi_id' }" in table
    assert "{ attr: 'finishpoi', name: 'finish_poi_id' }" in table
