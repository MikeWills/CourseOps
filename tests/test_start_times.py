"""A race's start time, shown beside its bib colour on the lead runners.

Waves start at different times - the Full at 07:00, the Half at 07:30 -
and "Purple bibs, started 07:00" is what someone reads a leader's time
against. Stored as the club typed it, a 24-hour clock time in the event's
own zone: it is a time on the day, not an instant, so there is nothing
for a phone in another zone to convert and get wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, importer, leaders, progress, users, web
from courseops.config import Settings
from courseops.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def race(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               center_lat=34.7, center_lon=-86.5)
    importer.stage_file(conn, event_id, FIXTURE)
    line = next(r["id"] for r in importer.pending_features(conn, event_id)
                if r["geom_type"] == "linestring")
    course_id, _, _ = importer.assign_course(conn, event_id, [line], name="Full")
    return conn, db_path, event_id, course_id


@pytest.mark.parametrize("typed, stored", [
    ("07:00", "07:00"),
    ("7:00", "07:00"),
    (" 7:05 ", "07:05"),
    ("13:30", "13:30"),
])
def test_a_start_time_is_stored_as_24_hour_hh_mm(race, typed, stored):
    conn, _, event_id, course_id = race
    saved = admin.update_course(conn, event_id, course_id, {"start_time": typed})
    assert saved["start_time"] == stored


@pytest.mark.parametrize("typed", ["7", "24:00", "07:60", "seven", "7:00 AM", ["x"]])
def test_a_start_time_that_is_not_a_clock_time_is_refused(race, typed):
    """Refused with a message, not stored: this is read against a leader's
    time on race morning, and a wrong one is worse than none."""
    conn, _, event_id, course_id = race
    with pytest.raises(ValueError):
        admin.update_course(conn, event_id, course_id, {"start_time": typed})


def test_emptying_it_clears_it(race):
    conn, _, event_id, course_id = race
    admin.update_course(conn, event_id, course_id, {"start_time": "07:00"})
    saved = admin.update_course(conn, event_id, course_id, {"start_time": ""})
    assert saved["start_time"] is None


def test_setting_it_leaves_the_bib_colour_alone(race):
    conn, _, event_id, course_id = race
    leaders.set_bib_color(conn, event_id, course_id, "#6633cc", "Purple")
    saved = admin.update_course(conn, event_id, course_id, {"start_time": "07:00"})
    assert (saved["bib_color"], saved["bib_color_name"]) == ("#6633cc", "Purple")


def test_the_leader_carries_it(race):
    conn, _, event_id, course_id = race
    admin.update_course(conn, event_id, course_id, {"start_time": "07:00"})
    index = progress.CourseIndex.for_event(conn, event_id)
    entry = leaders.for_event(conn, event_id, index)[0]
    assert entry.as_dict()["start_time"] == "07:00"


def test_an_existing_database_gets_the_column(tmp_path):
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    conn.execute("ALTER TABLE course DROP COLUMN start_time")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute("PRAGMA table_info(course)")}
    assert "start_time" in names


def test_set_in_setup_it_reaches_every_role(race):
    conn, db_path, event_id, course_id = race
    tokens = access.ensure_tokens(conn, event_id)
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    with TestClient(app) as client:
        client.post("/api/setup/login",
                    json={"username": "mike", "password": "a-long-enough-password"})
        response = client.post(f"/api/setup/events/{event_id}/courses/{course_id}",
                               json={"start_time": "07:00"})
        assert response.status_code == 200, response.text
        for role in access.ROLES:
            data = client.get(f"/api/m2026/{tokens[role]}/state").json()
            assert {l["start_time"] for l in data["leaders"]} == {"07:00"}, role


# --- the clients --------------------------------------------------------------

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")


def test_the_lead_runner_heading_names_the_start():
    head = APP_JS[APP_JS.index("head.className = 'leader-course'"):]
    head = head[:head.index("host.appendChild(head)")]
    assert "leader.start_time" in head


def test_the_courses_table_edits_it_and_saves_it_with_the_rest():
    table = SETUP_JS[SETUP_JS.index("$('course-table').innerHTML"):]
    table = table[:table.index("noun: 'course(s)'")]
    assert 'type="time"' in table and "data-start=" in table
    assert "{ attr: 'start', name: 'start_time' }" in table
