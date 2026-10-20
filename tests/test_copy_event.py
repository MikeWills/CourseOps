"""Copy an event (#240): rehearse on a copy, leave the original alone.

A copy takes the SETUP - courses, places, layers, roles, leaders, routes,
the roster - and none of what happened on the day or who was where.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, users
from courseops.config import Settings
from courseops.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"


@pytest.fixture
def world(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite3")
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")
    event = admin.create_event(
        conn, {"slug": "m2026", "name": "Marathon", "event_date": "2026-10-17",
               "timezone": "America/Chicago", "net_info": "Race RX 147.330",
               "center_lat": 44.1, "center_lon": -94.0}, org["id"])
    return conn, event["id"], org


def _seed(conn, eid):
    conn.execute("INSERT INTO poi (event_id, name, poi_type, lat, lon) "
                 "VALUES (?, 'Aid A', 'aid_station', 44.1, -94.0)", (eid,))
    poi = conn.execute("SELECT id FROM poi WHERE event_id = ?", (eid,)).fetchone()["id"]
    conn.execute("INSERT INTO course (event_id, name, geojson, start_poi_id) "
                 "VALUES (?, 'Full', '{}', ?)", (eid, poi))
    course = conn.execute("SELECT id FROM course WHERE event_id = ?", (eid,)).fetchone()["id"]
    conn.execute("INSERT INTO poi_course (event_id, poi_id, course_id) VALUES (?,?,?)",
                 (eid, poi, course))
    conn.execute("INSERT INTO course_stop (event_id, course_id, step, poi_id) "
                 "VALUES (?,?,1,?)", (eid, course, poi))
    db.upsert_roster_entry(conn, eid, "N0CALL-7", "Half-back", "rover")
    conn.execute("UPDATE roster SET poi_id = ?, op_status = 'active', "
                 "op_status_by = 'MW' WHERE station_key = 'N0CALL-7'", (poi,))
    admin.add_unit(conn, eid, {"badge": "4021", "lat": 44.1, "lon": -94.0})
    conn.execute("INSERT INTO incident (event_id, lat, lon, kind) "
                 "VALUES (?, 44.1, -94.0, 'pickup')", (eid,))
    return poi, course


def test_a_copy_takes_the_setup_under_its_own_name(world):
    conn, eid, org = world
    _seed(conn, eid)
    copy = admin.copy_event(conn, eid, {"slug": "rehearsal", "name": "Rehearsal"})
    assert copy["slug"] == "rehearsal" and copy["id"] != eid
    assert copy["timezone"] == "America/Chicago"
    assert copy["net_info"] == "Race RX 147.330"
    assert copy["organization_id"] == org["id"]
    n = copy["id"]
    for table in ("poi", "course", "poi_course", "course_stop", "poi_category",
                  "roster_role", "lead_division"):
        def count(i):
            return conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE event_id = ?",
                                (i,)).fetchone()["c"]
        assert count(n) == count(eid), table


def test_the_copy_points_at_its_own_rows_never_the_originals(world):
    conn, eid, _ = world
    poi, course = _seed(conn, eid)
    n = admin.copy_event(conn, eid, {"slug": "r", "name": "R"})["id"]
    new_poi = conn.execute("SELECT id FROM poi WHERE event_id = ?", (n,)).fetchone()["id"]
    new_course = conn.execute("SELECT * FROM course WHERE event_id = ?", (n,)).fetchone()
    assert new_poi != poi and new_course["id"] != course
    assert new_course["start_poi_id"] == new_poi
    stop = conn.execute("SELECT * FROM course_stop WHERE event_id = ?", (n,)).fetchone()
    assert (stop["course_id"], stop["poi_id"]) == (new_course["id"], new_poi)
    row = conn.execute("SELECT * FROM roster WHERE event_id = ? AND station_key = 'N0CALL-7'",
                       (n,)).fetchone()
    assert row["poi_id"] == new_poi


def test_a_copy_starts_the_day_clean(world):
    conn, eid, _ = world
    _seed(conn, eid)
    n = admin.copy_event(conn, eid, {"slug": "r", "name": "R"})["id"]
    row = conn.execute("SELECT * FROM roster WHERE event_id = ? AND station_key = 'N0CALL-7'",
                       (n,)).fetchone()
    assert row["op_status"] == "pending" and row["op_status_by"] is None
    # What happened on the day, and who stood where, stays with the original.
    assert conn.execute("SELECT COUNT(*) c FROM incident WHERE event_id = ?",
                        (n,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM roster WHERE event_id = ? AND "
                        "station_key = '4021'", (n,)).fetchone()["c"] == 0
    ev = conn.execute("SELECT * FROM event WHERE id = ?", (n,)).fetchone()
    assert ev["ingest_enabled"] == 0 and ev["tracker_token"] is None
    mine = {r["token"] for r in conn.execute(
        "SELECT token FROM access_token WHERE event_id = ?", (n,))}
    theirs = {r["token"] for r in conn.execute(
        "SELECT token FROM access_token WHERE event_id = ?", (eid,))}
    assert mine and not mine & theirs          # fresh links, not the original's


def test_the_original_is_untouched(world):
    conn, eid, _ = world
    _seed(conn, eid)

    def snap():
        return [tuple(r) for r in conn.execute(
            "SELECT * FROM roster WHERE event_id = ? ORDER BY id", (eid,))]
    before = snap()
    admin.copy_event(conn, eid, {"slug": "r", "name": "R"})
    assert before == snap()


def test_a_taken_or_bad_name_is_refused_and_leaves_nothing(world):
    conn, eid, _ = world
    with pytest.raises(ValueError):
        admin.copy_event(conn, eid, {"slug": "m2026", "name": "Again"})
    with pytest.raises(ValueError):
        admin.copy_event(conn, eid, {"slug": "", "name": "x"})
    assert conn.execute("SELECT COUNT(*) c FROM event").fetchone()["c"] == 1


def test_every_event_table_is_copied_or_deliberately_not():
    assert not set(admin.COPIED_TABLES) & set(admin.NOT_COPIED)
    assert set(admin.EXPORTED_TABLES) | set(admin.NOT_EXPORTED) == \
        set(admin.COPIED_TABLES) | set(admin.NOT_COPIED)


@pytest.fixture
def app(tmp_path):
    p = tmp_path / "t.sqlite3"
    conn = db.connect(p)
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")
    eid = admin.create_event(conn, {"slug": "m2026", "name": "Marathon"}, org["id"])["id"]
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    return create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                               db_path=p, log_level="WARNING")), eid


def test_the_setup_screen_can_copy(app):
    application, eid = app
    with TestClient(application) as client:
        client.post("/api/setup/login",
                    json={"username": "mike", "password": "a-long-enough-password"})
        r = client.post(f"/api/setup/events/{eid}/copy",
                        json={"slug": "rehearsal", "name": "Rehearsal"})
        bad = client.post(f"/api/setup/events/{eid}/copy",
                          json={"slug": "rehearsal", "name": "Again"})
    assert r.status_code == 201 and r.json()["slug"] == "rehearsal"
    assert bad.status_code == 400


def test_the_events_list_has_a_copy_button():
    js = (STATIC / "setup.js").read_text(encoding="utf-8")
    assert "data-copy" in js and "/copy`" in js


def test_links_list_in_the_order_the_roles_are_run(world):
    """Net Control, SAG, Liaison, Logistics, Staff - not alphabetical."""
    conn, eid, _ = world
    access.ensure_tokens(conn, eid)
    access.create_token(conn, eid, access.ROLE_NCS, "second")
    roles = [r["role"] for r in access.tokens_for_event(conn, eid)]
    assert roles == [access.ROLE_NCS, access.ROLE_NCS, access.ROLE_SAG,
                     access.ROLE_LIAISON, access.ROLE_LOGISTICS, access.ROLE_STAFF]


def test_copy_is_an_inline_form_with_its_own_icon_not_popups():
    """Every other setup form is inline; `copy` already means "copy this link"."""
    js = (STATIC / "setup.js").read_text(encoding="utf-8")
    html = (STATIC / "setup.html").read_text(encoding="utf-8")
    assert 'id="event-copy-form"' in html
    handler = js[js.index("$('event-copy-form').addEventListener"):]
    assert "prompt(" not in js[js.index("[data-copy]"):js.index("[data-archive]')")]
    assert "iconBtn('duplicate', {'data-copy'" in js
    assert "  duplicate: [" in js
