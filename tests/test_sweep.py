"""Sweep tracking, leg times and the finish estimate (#215; docs/sweep-tracking.md).

Same road as test_leader_route: out to J and back by I, H I J I K, with K the
finish. A sweep is a leader whose `kind` is 'sweep'.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from courseops import admin, categories, db, leaders, progress, report

STOPS = {"H": 0.020, "I": 0.040, "J": 0.080, "K": 0.010}


@pytest.fixture
def race(tmp_path):
    conn = db.connect(tmp_path / "sweep.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "sweep", "Sweep")
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
    admin.set_course_route(conn, event_id, course_id, [pid[n] for n in "HIJIK"])
    categories.add_lead_division(conn, event_id, "Sweep", kind="sweep")
    index = progress.CourseIndex.for_event(conn, event_id)
    return conn, event_id, course_id, index, pid


def entry(conn, event_id, index, division="sweep"):
    return next(e for e in leaders.for_event(conn, event_id, index)
                if e.division == division)


def sight(conn, event_id, course_id, pid, name, at, step, division="sweep"):
    row = leaders.record_sighting(conn, event_id, course_id, division,
                                  pid[name], step=step)
    conn.execute("UPDATE lead_sighting SET at = ? WHERE id = ?", (at, row["id"]))


# --- kind ---------------------------------------------------------------

def test_a_leader_is_a_plain_leader_unless_the_club_says_sweep(race):
    conn, event_id, *_ = race
    kinds = {r["key"]: r["kind"] for r in categories.lead_divisions(conn, event_id)}
    assert kinds["male"] == "leader" and kinds["sweep"] == "sweep"


def test_an_unknown_kind_is_refused(race):
    conn, event_id, *_ = race
    with pytest.raises(categories.CategoryError):
        categories.add_lead_division(conn, event_id, "Odd", kind="chaser")


def test_the_kind_column_is_added_to_an_old_database(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE lead_division (id INTEGER PRIMARY KEY,"
                 " event_id INTEGER, key TEXT, name TEXT, sort_order INTEGER)")
    conn.execute("INSERT INTO lead_division (event_id, key, name, sort_order)"
                 " VALUES (1, 'male', 'First male', 0)")
    db._apply_migrations(conn)
    assert conn.execute("SELECT kind FROM lead_division").fetchone()["kind"] == "leader"


def test_a_sweep_may_be_slower_than_a_leader_may_be():
    slow = 1609.344 / (40 * 60)             # 40:00 per mile
    lo, hi = leaders.pace_bounds("sweep")
    assert lo <= slow <= hi
    lo, hi = leaders.pace_bounds("leader")
    assert not lo <= slow <= hi
    assert leaders.pace_bounds("anything") == leaders.pace_bounds("leader")


# --- leg times ----------------------------------------------------------

def test_legs_are_the_time_between_consecutive_stops(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:20:00Z", 1)
    sight(conn, event_id, course_id, pid, "J", "2026-10-17T14:50:00Z", 2)
    legs = entry(conn, event_id, index).legs
    assert [l["poi_name"] for l in legs] == ["H", "I", "J"]
    assert [l["leg_seconds"] for l in legs] == [None, 1200, 1800]
    assert legs[1]["leg_m"] > 0 and legs[1]["leg_pace_mps"] > 0
    assert legs[0]["at"] == "2026-10-17T14:00:00Z"


def test_a_repeated_stop_is_two_legs_with_their_own_times(race):
    conn, event_id, course_id, index, pid = race
    for name, at, step in [("H", "14:00", 0), ("I", "14:20", 1),
                           ("J", "14:50", 2), ("I", "15:30", 3)]:
        sight(conn, event_id, course_id, pid, name, f"2026-10-17T{at}:00Z", step)
    legs = entry(conn, event_id, index).legs
    assert [l["step"] for l in legs] == [0, 1, 2, 3]
    assert legs[3]["leg_seconds"] == 2400 and legs[3]["leg_m"] > 0


def test_an_implausible_leg_keeps_its_time_but_shows_no_pace(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:00:30Z", 1)
    leg = entry(conn, event_id, index).legs[1]
    assert leg["leg_seconds"] == 30 and leg["leg_pace_mps"] is None


def test_no_route_means_no_legs_and_the_old_behaviour(race):
    conn, event_id, course_id, index, pid = race
    admin.set_course_route(conn, event_id, course_id, [])
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", None)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:20:00Z", None)
    e = entry(conn, event_id, index)
    assert e.legs == [] and e.finish_eta_at is None


# --- finish estimate ----------------------------------------------------

def test_the_finish_estimate_uses_the_running_pace_of_the_last_legs(race):
    conn, event_id, course_id, index, pid = race
    # A fast first leg then two slow ones: the average, not the last leg.
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:15:00Z", 1)
    sight(conn, event_id, course_id, pid, "J", "2026-10-17T15:00:00Z", 2)
    e = entry(conn, event_id, index)
    legs = e.legs
    total_m = legs[2]["distance_m"] - legs[0]["distance_m"]
    avg = total_m / (60 * 60)                # H->J took 60 minutes
    assert e.avg_pace_mps == pytest.approx(
        (legs[2]["distance_m"] - legs[1]["distance_m"]
         + legs[1]["leg_m"]) / 3600, rel=1e-6)
    assert e.avg_pace_mps != e.pace_mps
    assert e.avg_pace_mps == pytest.approx(avg, rel=1e-6)
    route = admin.course_route(conn, event_id, course_id)
    assert route[-1]["poi_id"] == pid["K"]
    assert e.finish_poi_name == "K"
    assert e.finish_distance_m == pytest.approx(e.finish_total_m - legs[2]["distance_m"])
    assert e.finish_eta_seconds == pytest.approx(e.finish_distance_m / e.avg_pace_mps)
    # ISO clock = last report + remaining time, for the browser to format.
    from datetime import datetime, timedelta, timezone
    last = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    got = datetime.strptime(e.finish_eta_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert abs((got - (last + timedelta(seconds=e.finish_eta_seconds))).total_seconds()) <= 1


def test_only_the_last_three_legs_count(race):
    conn, event_id, course_id, index, pid = race
    # About 12 min/mile throughout: out 17 and 33 minutes, back 67, in 17.
    for name, at, step in [("H", "14:00", 0), ("I", "14:17", 1),
                           ("J", "14:50", 2), ("I", "15:57", 3)]:
        sight(conn, event_id, course_id, pid, name, f"2026-10-17T{at}:00Z", step)
    legs = entry(conn, event_id, index).legs
    e = entry(conn, event_id, index)
    # Three legs back from the last report reach the first report exactly.
    assert e.avg_pace_mps == pytest.approx(
        (legs[3]["distance_m"] - legs[0]["distance_m"]) / (117 * 60), rel=1e-6)
    sight(conn, event_id, course_id, pid, "K", "2026-10-17T16:14:00Z", 4)
    e = entry(conn, event_id, index)
    legs = e.legs
    # The first leg (H to I) has dropped out of the window.
    assert e.avg_pace_mps == pytest.approx(
        (legs[4]["distance_m"] - legs[1]["distance_m"]) / (117 * 60), rel=1e-6)


def test_a_single_sighting_gives_no_estimate(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    e = entry(conn, event_id, index)
    assert e.avg_pace_mps is None and e.finish_eta_at is None
    assert e.finish_eta_seconds is None


def test_an_implausible_pace_gives_no_estimate(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:00:20Z", 1)
    e = entry(conn, event_id, index)
    assert e.avg_pace_mps is None and e.finish_eta_at is None


def test_a_walking_pace_is_fine_for_a_sweep_and_rejected_for_a_leader(race):
    conn, event_id, course_id, index, pid = race
    for division in ("sweep", "male"):
        sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0, division)
        sight(conn, event_id, course_id, pid, "I", "2026-10-17T15:00:00Z", 1, division)
    assert entry(conn, event_id, index, "sweep").finish_eta_at is not None
    assert entry(conn, event_id, index, "male").finish_eta_at is None
    assert entry(conn, event_id, index, "male").avg_pace_mps is None


def test_at_the_finish_there_is_nothing_left_to_estimate(race):
    conn, event_id, course_id, index, pid = race
    for name, at, step in [("H", "14:00", 0), ("I", "14:20", 1), ("J", "14:50", 2),
                           ("I", "15:20", 3), ("K", "15:40", 4)]:
        sight(conn, event_id, course_id, pid, name, f"2026-10-17T{at}:00Z", step)
    e = entry(conn, event_id, index)
    assert e.finished is True and e.next_poi_id is None
    assert e.finish_eta_at is None and e.finish_eta_seconds is None


def test_a_report_off_the_route_gives_no_estimate(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:20:00Z", 1)
    other = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'Elsewhere', 'aid_station', 0.00001, 0.05)",
        (event_id,)).lastrowid
    leaders.record_sighting(conn, event_id, course_id, "sweep", other)
    assert entry(conn, event_id, index).finish_eta_at is None


def test_the_payload_carries_the_estimate_and_the_kind(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:20:00Z", 1)
    data = entry(conn, event_id, index).as_dict()
    assert data["kind"] == "sweep"
    for key in ("legs", "avg_pace_mps", "finish_poi_name", "finish_eta_at",
                "finish_eta_seconds", "finished"):
        assert key in data
    assert data["finish_eta_at"].endswith("Z")


def test_reading_leaders_does_not_write(race):
    conn, event_id, course_id, index, pid = race
    sight(conn, event_id, course_id, pid, "H", "2026-10-17T14:00:00Z", 0)
    before = conn.total_changes
    leaders.for_event(conn, event_id, index)
    assert conn.total_changes == before


# --- the report -----------------------------------------------------------

def test_the_report_carries_the_leg_time_and_no_names(race):
    conn, event_id, course_id, index, pid = race
    leaders.record_sighting(conn, event_id, course_id, "sweep", pid["H"],
                            by="WX0MIK", step=0)
    conn.execute("UPDATE lead_sighting SET at = '2026-10-17T14:00:00Z'")
    sight(conn, event_id, course_id, pid, "I", "2026-10-17T14:20:00Z", 1)
    data = report.build(conn, event_id)
    run = next(r for r in data.leader_runs if r.leader == "Sweep")
    assert [p.leg_seconds for p in run.passings] == [None, 1200]
    html = report.render(data)
    assert "20 min" in html
    assert "WX0MIK" not in html


# --- the setup route -------------------------------------------------------

def test_setup_can_add_a_sweep(tmp_path):
    from courseops import setup_api
    tax = next(t for t in setup_api.TAXONOMIES if t.segment == "leaders")
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "t", "T")
    row = tax.add(conn, event_id, {"name": "Walking sweep", "kind": "sweep"})
    assert row["kind"] == "sweep"


# --- the pages ------------------------------------------------------------

def test_the_pages_send_and_read_the_sweep_fields():
    from pathlib import Path
    static = Path(__file__).parent.parent / "src" / "courseops" / "static"
    setup = (static / "setup.js").read_text(encoding="utf-8")
    assert "kind: $('leader-sweep').checked" in setup
    assert 'id="leader-sweep"' in (static / "setup.html").read_text(encoding="utf-8")
    app = (static / "app.js").read_text(encoding="utf-8")
    assert "function finishLabel" in app and "finish_eta_at" in app
    # Club-typed stop names reach innerHTML only through escapeHtml.
    assert "escapeHtml(placeName(leg.poi_id, leg.poi_name))" in app
