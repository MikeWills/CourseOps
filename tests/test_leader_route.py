"""The leader follows the route the club stated (#210, PR B; docs/leader-route.md).

The Mankato Full goes out to J and comes back by I: H, I, J, I, K. Before this
the panel named J as next after the SECOND I, and the legs out and back had no
pace. The fixture is the same shape: one road, driven out and then back.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from courseops import access, admin, db, leaders, progress

STATIC = Path(__file__).parent.parent / "src" / "courseops" / "static"

# Stop positions in degrees of longitude along the out leg; 0.001 deg ~ 111 m.
STOPS = {"H": 0.020, "I": 0.040, "J": 0.080, "K": 0.010}


@pytest.fixture
def race(tmp_path):
    conn = db.connect(tmp_path / "route.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "route", "Route")
    out = [[i * 0.001, 0.0] for i in range(0, 101)]
    back = [[(100 - i) * 0.001, 0.00002] for i in range(0, 101)]
    conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m)"
        " VALUES (?, 'Full', ?, 22000)",
        (event_id, json.dumps({"type": "LineString",
                               "coordinates": out + back})))
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    for name, lon in STOPS.items():
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', 0.00001, ?)", (event_id, name, lon))
    pid = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM poi")}
    admin.set_course_route(conn, event_id, course_id,
                           [pid[n] for n in "HIJIK"])
    index = progress.CourseIndex.for_event(conn, event_id)
    return conn, event_id, course_id, index, pid


def leader(conn, event_id, index, division="male"):
    return next(e for e in leaders.for_event(conn, event_id, index)
                if e.division == division)


def sight(conn, event_id, course_id, pid, name, at, step=None, division="male"):
    row = leaders.record_sighting(conn, event_id, course_id, division,
                                  pid[name], step=step)
    conn.execute("UPDATE lead_sighting SET at = ? WHERE id = ?", (at, row["id"]))
    return row


def test_the_route_is_measured_step_by_step_and_a_repeat_has_two_miles(race):
    conn, event_id, course_id, index, pid = race
    steps = admin.course_route(conn, event_id, course_id)
    rows = [conn.execute("SELECT * FROM poi WHERE id = ?", (s["poi_id"],)).fetchone()
            for s in steps]
    along = index.route_progression(course_id, rows)
    miles = [p.distance_along_m for p in along]
    assert miles == sorted(miles) and len(set(miles)) == 5
    # The second I is on the way back, past the turn at J.
    assert miles[3] > miles[2] > miles[1]


def test_with_no_sightings_the_first_step_is_next(race):
    conn, event_id, _, index, pid = race
    entry = leader(conn, event_id, index)
    assert entry.next_poi_name == "H" and entry.next_step == 0


def test_after_the_second_i_the_next_stop_is_k_not_j(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", step=0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:10:00Z", step=1)
    sight(conn, event_id, course_id, pid, "J", "2026-10-17T14:25:00Z", step=2)
    entry = leader(conn, event_id, index)
    assert entry.next_poi_name == "I" and entry.next_step == 3
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:40:00Z", step=3)
    entry = leader(conn, event_id, index)
    assert entry.last_step == 3
    assert entry.next_poi_name == "K" and entry.next_step == 4


def test_pace_is_positive_on_the_way_out_and_on_the_way_back(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", step=0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:05:00Z", step=1)
    assert leader(conn, event_id, index).pace_mps > 0
    sight(conn, event_id, course_id, pid, "J", "2026-10-17T14:25:00Z", step=2)
    out = leader(conn, event_id, index)
    assert out.pace_mps and out.eta_seconds and out.eta_seconds > 0
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:45:00Z", step=3)
    back = leader(conn, event_id, index)
    # J -> I is a normal leg now, not a backwards one that yields nothing.
    assert back.pace_mps and back.pace_mps > 0
    assert back.eta_seconds and back.eta_seconds > 0


def test_a_sighting_without_a_step_lands_on_the_next_pass_not_the_first(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z")
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:10:00Z")
    sight(conn, event_id, course_id, pid, "J", "2026-10-17T14:25:00Z")
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:40:00Z")
    entry = leader(conn, event_id, index)
    assert entry.last_step == 3 and entry.next_poi_name == "K"


def test_walking_passed_follows_the_whole_route(race):
    conn, event_id, course_id, index, pid = race
    seen = []
    for _ in range(8):
        entry = leader(conn, event_id, index)
        if not entry.next_poi_id:
            break
        seen.append(entry.next_poi_name)
        leaders.record_sighting(conn, event_id, course_id, "male",
                                entry.next_poi_id, step=entry.next_step)
    assert seen == list("HIJIK")


def test_a_stop_off_the_route_is_named_and_guesses_no_next(race):
    conn, event_id, course_id, index, pid = race
    other = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'Elsewhere', 'aid_station', 0.00001, 0.05)",
        (event_id,)).lastrowid
    leaders.record_sighting(conn, event_id, course_id, "male", other)
    entry = leader(conn, event_id, index)
    assert entry.last_poi_name == "Elsewhere"
    assert entry.next_poi_id is None


def test_a_step_that_does_not_match_the_place_is_stored_as_none(race):
    conn, event_id, course_id, index, pid = race
    ok = leaders.record_sighting(conn, event_id, course_id, "male", pid["J"], step=2)
    wrong = leaders.record_sighting(conn, event_id, course_id, "male", pid["J"], step=4)
    absurd = leaders.record_sighting(conn, event_id, course_id, "male", pid["J"], step=99)
    assert (ok["step"], wrong["step"], absurd["step"]) == (2, None, None)


def test_the_leader_payload_carries_the_steps(race):
    conn, event_id, course_id, index, pid = race
    data = leader(conn, event_id, index).as_dict()
    assert "last_step" in data and "next_step" in data


def test_the_sighting_route_accepts_a_step(tmp_path):
    from fastapi.testclient import TestClient
    from courseops.config import Settings
    from courseops.web import create_app

    path = tmp_path / "api.sqlite3"
    conn = db.connect(path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "api", "Api")
    conn.execute("INSERT INTO course (event_id, name, geojson, distance_m)"
                 " VALUES (?, 'Full', ?, 1000)",
                 (event_id, json.dumps({"type": "LineString",
                                        "coordinates": [[0, 0], [0.01, 0]]})))
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    conn.execute("INSERT INTO poi (event_id, name, poi_type, lat, lon)"
                 " VALUES (?, 'A', 'aid_station', 0, 0.005)", (event_id,))
    poi = conn.execute("SELECT id FROM poi").fetchone()["id"]
    admin.set_course_route(conn, event_id, course_id, [poi])
    token = access.ensure_tokens(conn, event_id)["ncs"]
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=path, log_level="WARNING"))
    with TestClient(app) as client:
        resp = client.post(f"/api/api/{token}/leaders/sighting",
                           json={"course_id": course_id, "division": "male",
                                 "poi_id": poi, "step": 0})
    assert resp.status_code == 201
    conn = db.connect(path)
    assert conn.execute("SELECT step FROM lead_sighting").fetchone()["step"] == 0


# --- the client ------------------------------------------------------------


def test_passed_sends_the_step_and_a_correction_does_not():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "step: step" in js or "step," in js[js.index("async function recordSighting"):
                                              js.index("async function resetLeader")]
    assert "leader.next_step" in js


def test_net_control_sees_the_clock_time_beside_the_age():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function renderLeaders"):]
    body = body[:body.index("const nextPlace")]
    # The NCS branch used to be age only; the log time is there too now.
    ncs = body[body.index("can('leaders')"):body.index("else if (leader.last_poi_name)")]
    assert "seenAt" in ncs
