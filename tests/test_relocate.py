"""Relocating an operator to another station when they wrap up (#211).

A post used to be one overwritten column, so sending someone from a water stop
that tears down early to a later one left no record of who staffed what until
when. The post history is append-only, like the status log, for the same
reason: it cannot be rebuilt afterwards.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from courseops import admin, db

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "r.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "r", "Relocate")
    db.seed_event_defaults(conn, event_id) if hasattr(db, "seed_event_defaults") else None
    for name, kind in (("A", "aid_station"), ("B", "aid_station"), ("Lot", "unstaffed_x")):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon) VALUES (?, ?, ?, 1, 1)",
            (event_id, name, kind))
    conn.execute(
        "INSERT OR IGNORE INTO poi_category (event_id, key, name, staffed)"
        " VALUES (?, 'unstaffed_x', 'Parking', 0)", (event_id,))
    pid = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM poi")}
    db.upsert_roster_entry(conn, event_id, "N0CALL-1", "Op", category="aid_station")
    db.assign_station_to_poi(conn, event_id, "N0CALL-1", pid["A"])
    db.set_op_status(conn, event_id, "N0CALL-1", "closed", "MW")
    return conn, event_id, pid


def test_relocating_posts_them_at_the_new_place_and_resets_the_status(event):
    conn, event_id, pid = event
    row = db.relocate_station(conn, event_id, "N0CALL-1", pid["B"], "MW")
    assert row["poi_id"] == pid["B"]
    assert row["op_status"] == "pending"      # not On station until NCS says so


def test_the_move_is_logged_with_from_and_to_and_by(event):
    conn, event_id, pid = event
    db.relocate_station(conn, event_id, "N0CALL-1", pid["B"], "MW")
    log = db.post_log(conn, event_id, "N0CALL-1")
    assert [(r["from_poi_id"], r["to_poi_id"], r["by"]) for r in log][-1] == (
        pid["A"], pid["B"], "MW")


def test_an_unstaffed_place_is_refused_and_nothing_changes(event):
    conn, event_id, pid = event
    with pytest.raises(ValueError, match="not staffed"):
        db.relocate_station(conn, event_id, "N0CALL-1", pid["Lot"], "MW")
    row = conn.execute("SELECT * FROM roster WHERE station_key = 'N0CALL-1'").fetchone()
    assert row["poi_id"] == pid["A"] and row["op_status"] == "closed"


def test_moving_to_the_place_they_are_already_at_is_refused(event):
    conn, event_id, pid = event
    with pytest.raises(ValueError, match="already"):
        db.relocate_station(conn, event_id, "N0CALL-1", pid["A"], "MW")


def test_the_history_follows_a_renamed_callsign_and_is_exported(event):
    conn, event_id, pid = event
    db.relocate_station(conn, event_id, "N0CALL-1", pid["B"], "MW")
    db.rename_station_key(conn, event_id, "N0CALL-1", "N0CALL-2")
    assert len(db.post_log(conn, event_id, "N0CALL-2")) >= 1
    assert "roster_post_log" in admin.EXPORTED_TABLES


def test_only_net_control_can_relocate_and_the_client_offers_it_there():
    from courseops import access
    api = (Path(__file__).resolve().parent.parent / "src" / "courseops" / "field_api.py"
           ).read_text(encoding="utf-8")
    route = api[api.index("/relocate"):]
    assert "CAP_STATIONS" in route[:300]
    assert access.CAP_STATIONS not in access.ROLE_CAPABILITIES[access.ROLE_SAG]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "relocate" in js and "can('stations')" in js
