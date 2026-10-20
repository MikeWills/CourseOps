"""Hand-placed unit pins: police, CSOs, Public Works (#196).

A roster entry whose position a person sets. The badge number is the
identity, NCS and Liaison move it, every pin says when and by whom, a quiet
unit is not an alarm, and the Staff link (the one that gets forwarded) never
receives any of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db
from courseops.config import Settings
from courseops.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"


@pytest.fixture
def setup(tmp_path):
    db_path = tmp_path / "u.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Marathon", center_lat=44.1, center_lon=-94.0)
    admin.save_roster_entry(conn, event_id, {
        "station_key": "4021", "display_label": "Officer 4021", "category": "rover",
        "tracked_by": "manual"})
    tokens = access.ensure_tokens(conn, event_id)
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    return app, tokens, db_path, event_id


def test_a_hand_placed_entry_is_not_an_aprs_callsign_and_never_alarms(setup):
    _, _, db_path, event_id = setup
    conn = db.connect(db_path)
    row = conn.execute("SELECT * FROM roster WHERE station_key = '4021'").fetchone()
    assert row["tracked_by"] == "manual"
    assert row["expects_aprs"] == 0          # a quiet officer is normal
    assert "4021" not in db.tracked_station_keys(conn, event_id)
    assert "4021" not in db.rostered_base_callsigns(conn, event_id)


def test_the_badge_is_normalised_like_a_designator(setup):
    _, _, db_path, event_id = setup
    conn = db.connect(db_path)
    admin.save_roster_entry(conn, event_id, {
        "station_key": "pw-12a", "display_label": "Public Works 12A",
        "category": "rover", "tracked_by": "manual"})
    assert conn.execute("SELECT 1 FROM roster WHERE station_key = 'PW12A'").fetchone()


def test_ncs_and_liaison_can_move_a_unit_and_the_pin_says_who_and_when(setup):
    app, tokens, db_path, _ = setup
    with TestClient(app) as client:
        done = client.post(f"/api/m2026/{tokens['liaison']}/units/4021/position",
                           json={"lat": 44.1, "lon": -94.0, "changed_by": "LZ"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert done.status_code == 200
    pos = next(p for p in data["positions"] if p["station_key"] == "4021")
    assert (pos["lat"], pos["lon"]) == (44.1, -94.0)
    assert pos["comment"] == "LZ" and pos["received_at"]


def test_other_roles_cannot_move_a_unit(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        for role in ("sag", "logistics", "staff"):
            r = client.post(f"/api/m2026/{tokens[role]}/units/4021/position",
                            json={"lat": 44.1, "lon": -94.0})
            assert r.status_code == 403, role


def test_only_a_hand_placed_entry_can_be_placed_by_hand(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    conn.close()
    with TestClient(app) as client:
        r = client.post(f"/api/m2026/{tokens['ncs']}/units/N0CALL-7/position",
                        json={"lat": 44.1, "lon": -94.0})
        bad = client.post(f"/api/m2026/{tokens['ncs']}/units/4021/position",
                          json={"lat": 91, "lon": 0})
    assert r.status_code == 400 and bad.status_code == 400


def test_staff_never_receive_a_unit_in_the_snapshot_or_on_the_socket(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/units/4021/position",
                    json={"lat": 44.1, "lon": -94.0})
        staff = client.get(f"/api/m2026/{tokens['staff']}/state").json()
        logistics = client.get(f"/api/m2026/{tokens['logistics']}/state").json()
        with client.websocket_connect(f"/ws/m2026/{tokens['staff']}") as ws_staff, \
                client.websocket_connect(f"/ws/m2026/{tokens['liaison']}") as ws_liaison:
            client.post(f"/api/m2026/{tokens['ncs']}/units/4021/position",
                        json={"lat": 44.11, "lon": -94.0})
            seen = ws_liaison.receive_json()
            # Staff's queue must hold nothing about the unit; the next thing
            # it can receive is whatever we send now.
            client.post(f"/api/m2026/{tokens['ncs']}/station/4021/status",
                        json={"op_status": "active"})
            staff_next = ws_staff.receive_json()
    assert not any(e["station_key"] == "4021" for e in staff["roster"])
    assert not any(p["station_key"] == "4021" for p in staff["positions"])
    assert any(p["station_key"] == "4021" for p in logistics["positions"])
    assert seen["type"] == "position" and seen["station_key"] == "4021"
    assert staff_next.get("station_key") != "4021"


def test_capabilities_are_split_view_for_the_field_move_for_two():
    caps = access.ROLE_CAPABILITIES
    assert access.CAP_UNIT_MOVE in caps[access.ROLE_NCS]
    assert access.CAP_UNIT_MOVE in caps[access.ROLE_LIAISON]
    for role in (access.ROLE_SAG, access.ROLE_LOGISTICS, access.ROLE_STAFF):
        assert access.CAP_UNIT_MOVE not in caps[role]
    assert access.CAP_UNIT_VIEW not in caps[access.ROLE_STAFF]
    for role in (access.ROLE_NCS, access.ROLE_SAG, access.ROLE_LIAISON, access.ROLE_LOGISTICS):
        assert access.CAP_UNIT_VIEW in caps[role]


def test_the_client_draws_a_pill_with_the_badge_age_and_who_and_drags_only_for_movers():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "stn--unit" in js and "'manual'" in js
    assert "can('unit_move')" in js and "units/" in js
    popup = js[js.index("function stationPopup"):js.index("function stationVisible")]
    assert "Set" in popup and "by" in popup
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".stn--unit" in css


def test_setup_offers_hand_placed_as_a_way_of_tracking():
    setup_js = (STATIC / "setup.js").read_text(encoding="utf-8")
    html = (STATIC / "setup.html").read_text(encoding="utf-8")
    assert 'value="manual"' in html
    assert "'manual'" in setup_js


# --- units are made and removed from the live pages, not from setup --------


def test_liaison_adds_a_unit_from_the_field_with_no_setup_access(setup):
    """The people who place police and Public Works units hold a role link and
    no setup account, and do not know who is working until the day, so the
    unit must be creatable from the live app."""
    app, tokens, db_path, event_id = setup
    with TestClient(app) as client:
        made = client.post(f"/api/m2026/{tokens['liaison']}/units",
                           json={"badge": "pw 12a", "kind": "Public Works",
                                 "lat": 44.1, "lon": -94.0, "changed_by": "LZ"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert made.status_code == 200
    entry = next(e for e in data["roster"] if e["station_key"] == "PW12A")
    assert entry["tracked_by"] == "manual" and entry["expects_aprs"] == 0
    assert entry["display_label"] == "Public Works PW12A"
    pos = next(p for p in data["positions"] if p["station_key"] == "PW12A")
    assert pos["comment"] == "LZ"


def test_adding_an_existing_badge_moves_it_instead_of_making_two(setup):
    app, tokens, db_path, event_id = setup
    with TestClient(app) as client:
        for lat in (44.1, 44.2):
            client.post(f"/api/m2026/{tokens['ncs']}/units",
                        json={"badge": "4021", "lat": lat, "lon": -94.0})
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM roster WHERE station_key = '4021'"
                        ).fetchone()["n"] == 1
    assert conn.execute("SELECT lat FROM position WHERE station_key = '4021'"
                        ).fetchone()["lat"] == 44.2


def test_a_unit_cannot_take_over_a_real_callsign_or_a_bad_badge(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "N0CALL", "Op", "rover")
    conn.close()
    with TestClient(app) as client:
        clash = client.post(f"/api/m2026/{tokens['ncs']}/units",
                            json={"badge": "N0CALL", "lat": 44.1, "lon": -94.0})
        empty = client.post(f"/api/m2026/{tokens['ncs']}/units",
                            json={"badge": " ", "lat": 44.1, "lon": -94.0})
        denied = client.post(f"/api/m2026/{tokens['sag']}/units",
                             json={"badge": "9", "lat": 44.1, "lon": -94.0})
    assert clash.status_code == 400 and empty.status_code == 400
    assert denied.status_code == 403


def test_a_unit_can_be_stood_down_from_the_field_and_only_a_unit(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    conn.close()
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/units",
                    json={"badge": "4021", "lat": 44.1, "lon": -94.0})
        refused = client.post(f"/api/m2026/{tokens['ncs']}/units/N0CALL-7/remove")
        denied = client.post(f"/api/m2026/{tokens['logistics']}/units/4021/remove")
        gone = client.post(f"/api/m2026/{tokens['liaison']}/units/4021/remove")
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert refused.status_code == 400 and denied.status_code == 403
    assert gone.status_code == 200
    assert not any(e["station_key"] == "4021" for e in data["roster"])
    assert not any(p["station_key"] == "4021" for p in data["positions"])


def test_the_live_page_has_the_add_form_and_a_remove_button():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="unit-add-form"' in html and 'id="unit-badge"' in html
    assert "post('units'" in js and "/remove" in js
    assert "unit-add-form" in js and "can('unit_move')" in js


def test_units_have_their_own_section_above_the_stations():
    """Found and moved without hunting through the roster."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert html.index('id="unit-section"') < html.index('id="station-section"')
    section = html[html.index('id="unit-section"'):html.index('id="station-section"')]
    assert 'id="unit-add-form"' in section and 'id="unit-list"' in section
    assert 'id="unit-add-form"' not in html[html.index('id="station-section"'):]
    plan = js[js.index("const SIDE_PANEL_BY_ROLE"):js.index("function sidePanelPlan")]
    # Every role that is sent units lists the section; Staff, never sent them, does not.
    for role in ("ncs:", "logistics:", "liaison:", "sag:"):
        assert "unit-section" in plan[plan.index(role):].split("\n  ")[0] or \
            "unit-section" in plan[plan.index(role):plan.index(role) + 400]
    staff = plan[plan.index("staff:"):plan.index("staff:") + 80]
    assert "unit-section" not in staff
    render = js[js.index("function renderStations"):]
    assert "isUnit(stationKey) ? unitHost : host" in render
