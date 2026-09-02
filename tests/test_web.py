"""Web layer: access control, state snapshot, and the live feed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import access, db, importer
from courseops.config import Settings
from courseops.parser import parse_packet
from courseops.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def setup(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon", center_lat=34.7,
                               center_lon=-86.5)

    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    db.upsert_roster_entry(conn, event_id, "W1AW-9", "SAG 1", "sag")
    db.upsert_roster_entry(conn, event_id, "KI4HMD-1", "Aid 4", "aid_station",
                           expects_aprs=False)

    importer.stage_file(conn, event_id, FIXTURE)
    lines = [r["id"] for r in importer.pending_features(conn, event_id)
             if r["geom_type"] == "linestring"]
    points = [r["id"] for r in importer.pending_features(conn, event_id)
              if r["geom_type"] == "point"]
    importer.assign_course(conn, event_id, [lines[0]], name="Half", color="#cc3333")
    importer.assign_poi(conn, event_id, points[0], "aid_station",
                        what3words="filled.count.soap")

    tokens = access.ensure_tokens(conn, event_id)
    conn.close()

    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    app = create_app(settings)
    return app, tokens, db_path, event_id


# --- access control ---------------------------------------------------------

def test_no_public_landing_page(setup):
    app, _, _, _ = setup
    with TestClient(app) as client:
        assert client.get("/").status_code == 404


def test_valid_token_serves_the_map(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.get(f"/e/m2026/{tokens['ncs']}")
        assert response.status_code == 200
        assert "<div id=\"map\">" in response.text


def test_bad_token_is_404_not_403(setup):
    """403 would confirm the event exists. It must not."""
    app, _, _, _ = setup
    with TestClient(app) as client:
        assert client.get("/e/m2026/not-a-real-token").status_code == 404
        assert client.get("/api/m2026/not-a-real-token/state").status_code == 404


def test_token_is_scoped_to_its_event(setup):
    """A valid token must be useless against a different event."""
    app, tokens, db_path, _ = setup
    conn = db.connect(db_path)
    db.create_event(conn, "other", "Other Event")
    conn.close()

    with TestClient(app) as client:
        assert client.get(f"/api/other/{tokens['ncs']}/state").status_code == 404


def test_revoked_token_stops_working(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    row = next(r for r in access.tokens_for_event(conn, event_id)
               if r["token"] == tokens["liaison"])
    access.revoke(conn, row["id"])
    conn.close()

    with TestClient(app) as client:
        assert client.get(f"/api/m2026/{tokens['liaison']}/state").status_code == 404
        assert client.get(f"/api/m2026/{tokens['ncs']}/state").status_code == 200


def test_roles_differ_on_write_permission(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
        liaison = client.get(f"/api/m2026/{tokens['liaison']}/state").json()

    assert ncs["role"] == "ncs" and ncs["can_write"] is True
    assert liaison["role"] == "liaison" and liaison["can_write"] is False


# --- state snapshot ---------------------------------------------------------

def test_state_carries_everything_needed_to_draw(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert data["event"]["name"] == "Spring Marathon"
    assert len(data["courses"]) == 1
    assert data["courses"][0]["color"] == "#cc3333"
    assert data["courses"][0]["geojson"]["type"] == "LineString"
    assert len(data["pois"]) == 1
    assert data["pois"][0]["what3words"] == "filled.count.soap"
    assert len(data["roster"]) == 3


def test_state_keeps_speed_metric_on_the_wire(setup):
    """Storage and transport stay metric; the browser converts for display."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    report = parse_packet(
        "N0CALL-7>APRS,TCPIP*,qAC,X:!3444.00N/08635.00W>180/030/A=001000"
    )
    db.insert_position(conn, event_id, report)
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    position = data["positions"][0]
    assert "speed_kmh" in position and "speed_mph" not in position


def test_no_aprs_roster_entry_is_flagged_in_state(setup):
    """The client needs expects_aprs to avoid alerting on a silent aid station."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    aid = next(r for r in data["roster"] if r["station_key"] == "KI4HMD-1")
    assert aid["expects_aprs"] == 0


def test_state_exposes_staleness_thresholds(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert data["thresholds"]["stale_after_s"] < data["thresholds"]["silent_after_s"]


# --- live feed --------------------------------------------------------------

def test_websocket_rejects_a_bad_token(setup):
    app, _, _, _ = setup
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/m2026/nope"):
                pass


def test_websocket_accepts_a_valid_token(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['liaison']}") as ws:
            assert app.state.hub.subscriber_count(1) == 1
            ws.close()


def test_websocket_subscription_is_released_on_disconnect(setup):
    app, tokens, _, event_id = setup
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['ncs']}"):
            pass
    assert app.state.hub.subscriber_count(event_id) == 0


# --- static assets ----------------------------------------------------------

def test_client_assets_are_served(setup):
    app, _, _, _ = setup
    with TestClient(app) as client:
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200


# --- roles ------------------------------------------------------------------

def test_three_roles_each_get_their_own_link(setup):
    """Liaison and Logistics are different teams, so one can be revoked
    without cutting off the other."""
    _, tokens, _, _ = setup
    assert set(tokens) == {"ncs", "liaison", "logistics"}
    assert len(set(tokens.values())) == 3


def test_logistics_is_read_only(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['logistics']}/state").json()
    assert data["role"] == "logistics"
    assert data["role_label"] == "Logistics"
    assert data["can_write"] is False


def test_revoking_one_field_role_leaves_the_other_working(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    row = next(r for r in access.tokens_for_event(conn, event_id)
               if r["token"] == tokens["logistics"])
    access.revoke(conn, row["id"])
    conn.close()

    with TestClient(app) as client:
        assert client.get(f"/api/m2026/{tokens['logistics']}/state").status_code == 404
        assert client.get(f"/api/m2026/{tokens['liaison']}/state").status_code == 200


# --- operational status (Phase 4) -------------------------------------------

def status_url(token, station_key="N0CALL-7"):
    return f"/api/m2026/{token}/station/{station_key}/status"


def test_ncs_can_set_operational_status(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.post(status_url(tokens["ncs"]),
                               json={"op_status": "active", "changed_by": "MW"})
    assert response.status_code == 200
    body = response.json()
    assert body["op_status"] == "active"
    assert body["op_status_label"] == "Rolling"     # sweep wording
    assert body["op_status_by"] == "MW"
    assert body["op_status_at"] is not None


def test_read_only_roles_cannot_write(setup):
    """The whole point of the role split."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        for role in ("liaison", "logistics"):
            response = client.post(status_url(tokens[role]), json={"op_status": "active"})
            assert response.status_code == 403, role
            assert "read-only" in response.json()["detail"]


def test_write_with_a_bad_token_is_404_not_403(setup):
    """An invalid token must not reveal that the event exists, even on a write."""
    app, _, _, _ = setup
    with TestClient(app) as client:
        response = client.post(status_url("not-a-token"), json={"op_status": "active"})
    assert response.status_code == 404


def test_unknown_status_is_rejected(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.post(status_url(tokens["ncs"]), json={"op_status": "banana"})
    assert response.status_code == 400
    assert "banana" in response.json()["detail"]


def test_status_for_a_station_not_on_the_roster_is_rejected(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.post(status_url(tokens["ncs"], "ZZ9ZZZ-1"),
                               json={"op_status": "active"})
    assert response.status_code == 400


def test_status_change_is_broadcast_to_every_viewer(setup):
    """A read-only role must see NCS's change without reloading."""
    app, tokens, _, event_id = setup
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['liaison']}") as ws:
            client.post(status_url(tokens["ncs"]), json={"op_status": "closed"})
            message = ws.receive_json()

    assert message["type"] == "station_status"
    assert message["station_key"] == "N0CALL-7"
    assert message["op_status"] == "closed"
    assert message["op_status_label"] == "Finished"


def test_state_carries_both_status_axes_independently(setup):
    """expects_aprs=0 plus op_status=active is a healthy row, not a conflict."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(status_url(tokens["ncs"], "KI4HMD-1"), json={"op_status": "active"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    aid = next(r for r in data["roster"] if r["station_key"] == "KI4HMD-1")
    assert aid["op_status"] == "active"
    assert aid["op_status_label"] == "On station"   # aid station wording
    assert aid["expects_aprs"] == 0                 # and still not APRS-tracked


def test_category_specific_wording(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        sweep = client.post(status_url(tokens["ncs"], "N0CALL-7"),
                            json={"op_status": "closed"}).json()
        aid = client.post(status_url(tokens["ncs"], "KI4HMD-1"),
                          json={"op_status": "closed"}).json()
    assert sweep["op_status_label"] == "Finished"
    assert aid["op_status_label"] == "Torn down"


def test_initials_are_truncated_not_trusted(setup):
    """A log annotation for shift handover, never identity."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        body = client.post(status_url(tokens["ncs"]),
                           json={"op_status": "active",
                                 "changed_by": "x" * 50}).json()
    assert len(body["op_status_by"]) == 12


# --- icons and home screen install ------------------------------------------

def test_icon_set_is_served(setup):
    """iOS ignores SVG and the manifest for home screen icons, so the PNGs are
    not redundant with favicon.svg."""
    app, _, _, _ = setup
    required = [
        "favicon.svg", "favicon.ico", "favicon-16.png", "favicon-32.png",
        "apple-touch-icon.png", "icon-192.png", "icon-512.png",
        "icon-maskable-192.png", "icon-maskable-512.png",
    ]
    with TestClient(app) as client:
        for name in required:
            assert client.get(f"/static/{name}").status_code == 200, name


def test_manifest_start_url_carries_the_token(setup):
    """A static start_url would install a home screen shortcut to a 404, because
    the app has no tokenless entry point."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.get(f"/api/m2026/{tokens['ncs']}/manifest.webmanifest")
    assert response.status_code == 200
    body = response.json()
    assert body["start_url"] == f"/e/m2026/{tokens['ncs']}"
    assert body["display"] == "standalone"


def test_manifest_short_name_is_the_role(setup):
    """Home screen labels truncate; the role is the useful half when someone
    holds links for two roles."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/manifest.webmanifest").json()
        log = client.get(f"/api/m2026/{tokens['logistics']}/manifest.webmanifest").json()
    assert ncs["short_name"] == "Net Control"
    assert log["short_name"] == "Logistics"


def test_manifest_declares_maskable_icons(setup):
    """Android crops to a launcher-chosen shape and guarantees only the central
    80%, so a maskable variant with a safe zone is required."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        body = client.get(f"/api/m2026/{tokens['ncs']}/manifest.webmanifest").json()
    purposes = {icon["purpose"] for icon in body["icons"]}
    assert purposes == {"any", "maskable"}


def test_manifest_requires_a_valid_token(setup):
    app, _, _, _ = setup
    with TestClient(app) as client:
        assert client.get("/api/m2026/nope/manifest.webmanifest").status_code == 404


def test_map_page_injects_the_manifest_link(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        html = client.get(f"/e/m2026/{tokens['ncs']}").text
    assert f'href="/api/m2026/{tokens["ncs"]}/manifest.webmanifest"' in html
    assert "__MANIFEST_URL__" not in html
    assert 'rel="apple-touch-icon"' in html


# --- course-relative position (Phase 5) -------------------------------------

def test_state_includes_course_position(setup):
    """The mile figure is what the net speaks, so it travels with the position."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    # A point on the imported Half course from the fixture.
    conn.execute(
        "INSERT INTO position (event_id, station_key, received_at, lat, lon, raw)"
        " VALUES (?, 'N0CALL-7', '2026-04-11T14:00:00Z', 34.732, -86.575, 'x')",
        (event_id,),
    )
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    located = data["positions"][0]["course_position"]
    assert located is not None
    assert located["course_name"] == "Half"
    assert located["distance_along_m"] >= 0
    assert located["distance_along_m"] + located["remaining_m"] == pytest.approx(
        located["course_length_m"], abs=1.0
    )


def test_station_far_from_any_course_reports_none(setup):
    """No number beats a plausible wrong one; someone acts on this."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO position (event_id, station_key, received_at, lat, lon, raw)"
        " VALUES (?, 'W1AW-9', '2026-04-11T14:00:00Z', 40.0, -100.0, 'x')",
        (event_id,),
    )
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    far = next(p for p in data["positions"] if p["station_key"] == "W1AW-9")
    assert far["course_position"] is None


# --- incidents (Phase 6) ----------------------------------------------------

def incidents_url(token):
    return f"/api/m2026/{token}/incidents"


def test_ncs_can_open_an_incident(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        response = client.post(incidents_url(tokens["ncs"]), json={
            "lat": 34.732, "lon": -86.575, "bib": "1432",
            "note": "unable to continue", "changed_by": "MW",
        })
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "reported"
    assert body["status_label"] == "Reported"
    assert body["bib"] == "1432"


def test_read_only_roles_cannot_open_or_change_incidents(setup):
    """Liaison and Logistics see incidents but cannot touch them in v1."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        created = client.post(incidents_url(tokens["ncs"]),
                              json={"lat": 34.73, "lon": -86.57}).json()
        for role in ("liaison", "logistics"):
            assert client.post(incidents_url(tokens[role]),
                               json={"lat": 34.73, "lon": -86.57}).status_code == 403
            assert client.post(
                f"{incidents_url(tokens[role])}/{created['id']}/status",
                json={"status": "closed"},
            ).status_code == 403


def test_incident_appears_in_state_with_a_course_position(setup):
    """"bib 1432, mile 0.4 of Half" is what gets said on the radio."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(incidents_url(tokens["ncs"]),
                    json={"lat": 34.732, "lon": -86.575, "bib": "1432"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert len(data["incidents"]) == 1
    entry = data["incidents"][0]
    assert entry["bib"] == "1432"
    assert entry["course_position"]["course_name"] == "Half"


def test_incidents_are_visible_to_read_only_roles(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(incidents_url(tokens["ncs"]),
                    json={"lat": 34.732, "lon": -86.575, "bib": "1432"})
        data = client.get(f"/api/m2026/{tokens['logistics']}/state").json()
    assert len(data["incidents"]) == 1


def test_status_change_is_broadcast(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        created = client.post(incidents_url(tokens["ncs"]),
                              json={"lat": 34.732, "lon": -86.575}).json()
        with client.websocket_connect(f"/ws/m2026/{tokens['liaison']}") as ws:
            client.post(f"{incidents_url(tokens['ncs'])}/{created['id']}/status",
                        json={"status": "en_route", "changed_by": "MW"})
            message = ws.receive_json()

    assert message["type"] == "incident"
    assert message["change"] == "status"
    assert message["status"] == "en_route"


def test_incident_log_is_readable_by_every_role(setup):
    """The log is what a shift handover reads."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        created = client.post(incidents_url(tokens["ncs"]),
                              json={"lat": 34.732, "lon": -86.575,
                                    "changed_by": "MW"}).json()
        client.post(f"{incidents_url(tokens['ncs'])}/{created['id']}/status",
                    json={"status": "picked_up", "changed_by": "AB"})
        entries = client.get(
            f"{incidents_url(tokens['liaison'])}/{created['id']}/log"
        ).json()["entries"]

    assert [e["action"] for e in entries] == ["created", "status"]
    assert [e["by"] for e in entries] == ["MW", "AB"]


def test_bad_incident_payloads_are_rejected(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        assert client.post(incidents_url(tokens["ncs"]),
                           json={"lat": 91.0, "lon": 0.0}).status_code == 400
        assert client.post(incidents_url(tokens["ncs"]),
                           json={"lon": 0.0}).status_code == 400
        created = client.post(incidents_url(tokens["ncs"]),
                              json={"lat": 34.73, "lon": -86.57}).json()
        assert client.post(
            f"{incidents_url(tokens['ncs'])}/{created['id']}/status",
            json={"status": "banana"},
        ).status_code == 400


def test_editing_an_incident_updates_and_logs(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        created = client.post(incidents_url(tokens["ncs"]),
                              json={"lat": 34.73, "lon": -86.57}).json()
        updated = client.post(
            f"{incidents_url(tokens['ncs'])}/{created['id']}",
            json={"bib": "0917", "assigned_to": "SAG 1", "changed_by": "MW"},
        ).json()

    assert updated["bib"] == "0917"
    assert updated["assigned_to"] == "SAG 1"


# --- lead runners -----------------------------------------------------------

def test_leaders_appear_in_state_per_course_and_division(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert {e["division"] for e in data["leaders"]} == {"male", "female"}
    assert [d["label"] for d in data["divisions"]] == ["First male", "First female"]


def test_recording_a_sighting_broadcasts_to_read_only_roles(setup):
    """Aid stations need to know the leader is coming; Logistics watches too."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    poi_id = conn.execute("SELECT id FROM poi").fetchone()["id"]
    conn.close()

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['logistics']}") as ws:
            response = client.post(
                f"/api/m2026/{tokens['ncs']}/leaders/sighting",
                json={"course_id": course_id, "division": "male",
                      "poi_id": poi_id, "bib": "101", "changed_by": "MW"},
            )
            message = ws.receive_json()

    assert response.status_code == 201
    assert message["type"] == "leaders"
    male = next(e for e in message["leaders"] if e["division"] == "male")
    assert male["bib"] == "101"
    assert male["last_by"] == "MW"


def test_read_only_roles_cannot_record_a_sighting(setup):
    app, tokens, db_path, _ = setup
    conn = db.connect(db_path)
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    poi_id = conn.execute("SELECT id FROM poi").fetchone()["id"]
    conn.close()

    with TestClient(app) as client:
        for role in ("liaison", "logistics"):
            assert client.post(
                f"/api/m2026/{tokens[role]}/leaders/sighting",
                json={"course_id": course_id, "division": "male", "poi_id": poi_id},
            ).status_code == 403


def test_a_mis_tap_can_be_undone_over_the_api(setup):
    app, tokens, db_path, _ = setup
    conn = db.connect(db_path)
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    poi_id = conn.execute("SELECT id FROM poi").fetchone()["id"]
    conn.close()

    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/leaders/sighting",
                    json={"course_id": course_id, "division": "male",
                          "poi_id": poi_id})
        undone = client.post(f"/api/m2026/{tokens['ncs']}/leaders/undo",
                             json={"course_id": course_id, "division": "male"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert undone.json()["removed"] is True
    male = next(e for e in data["leaders"] if e["division"] == "male")
    assert male["last_poi_id"] is None


def test_bib_colour_can_be_set_over_the_api(setup):
    app, tokens, db_path, _ = setup
    conn = db.connect(db_path)
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    conn.close()

    with TestClient(app) as client:
        response = client.post(
            f"/api/m2026/{tokens['ncs']}/course/{course_id}/bib-color",
            json={"bib_color": "#ffcc00", "bib_color_name": "Yellow"},
        )
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert response.status_code == 200
    male = next(e for e in data["leaders"] if e["division"] == "male")
    assert male["bib_color"] == "#ffcc00"
    assert male["bib_color_name"] == "Yellow"


def test_bad_sighting_payloads_are_rejected(setup):
    app, tokens, db_path, _ = setup
    conn = db.connect(db_path)
    course_id = conn.execute("SELECT id FROM course").fetchone()["id"]
    conn.close()

    with TestClient(app) as client:
        assert client.post(f"/api/m2026/{tokens['ncs']}/leaders/sighting",
                           json={"course_id": course_id, "division": "male",
                                 "poi_id": 9999}).status_code == 400
        assert client.post(f"/api/m2026/{tokens['ncs']}/leaders/sighting",
                           json={"course_id": course_id, "division": "",
                                 "poi_id": 1}).status_code == 400


# --- operational status history ---------------------------------------------

def test_status_changes_are_logged_and_never_overwritten(setup):
    """roster.op_status keeps only the current value, and that history cannot be
    reconstructed later - so it is captured as it happens."""
    app, tokens, _, _ = setup
    url = f"/api/m2026/{tokens['ncs']}/station/N0CALL-7/status"
    with TestClient(app) as client:
        client.post(url, json={"op_status": "active", "changed_by": "MW"})
        client.post(url, json={"op_status": "closed", "changed_by": "MW"})
        client.post(url, json={"op_status": "active", "changed_by": "AB"})
        entries = client.get(
            f"/api/m2026/{tokens['ncs']}/station-log?station_key=N0CALL-7"
        ).json()["entries"]

    # The reopening is the case the roster row alone could never express.
    assert [(e["from_status"], e["to_status"], e["by"]) for e in entries] == [
        ("pending", "active", "MW"),
        ("active", "closed", "MW"),
        ("closed", "active", "AB"),
    ]


def test_the_whole_event_log_is_readable_for_handover(setup):
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/station/N0CALL-7/status",
                    json={"op_status": "active", "changed_by": "MW"})
        client.post(f"/api/m2026/{tokens['ncs']}/station/KI4HMD-1/status",
                    json={"op_status": "active", "changed_by": "MW"})
        entries = client.get(f"/api/m2026/{tokens['ncs']}/station-log").json()["entries"]

    assert {e["station_key"] for e in entries} == {"N0CALL-7", "KI4HMD-1"}


def test_the_status_log_is_readable_by_read_only_roles(setup):
    """An incoming operator needs the handover regardless of write access."""
    app, tokens, _, _ = setup
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/station/N0CALL-7/status",
                    json={"op_status": "active", "changed_by": "MW"})
        response = client.get(f"/api/m2026/{tokens['liaison']}/station-log")

    assert response.status_code == 200
    assert len(response.json()["entries"]) == 1


def test_the_status_log_needs_a_valid_token(setup):
    app, _, _, _ = setup
    with TestClient(app) as client:
        assert client.get("/api/m2026/nope/station-log").status_code == 404


# --- SSID mismatch alerts ---------------------------------------------------
#
# The scenario: WX0MIK signs up as -1, beacons -5, and runs a digipeater on -7.
# Surfaced in the UI rather than left to a command, because the failure is
# silent and anything requiring memory gets forgotten on race morning.

def _heard(conn, event_id, station_key, symbol=(None, None), n=1):
    for _ in range(n):
        conn.execute(
            "INSERT INTO position (event_id, station_key, received_at, lat, lon,"
            " symbol_table, symbol_code, raw) VALUES (?, ?, ?, ?, ?, ?, ?, 'x')",
            (event_id, station_key, "2026-04-11T14:00:00Z", 34.73, -86.57,
             symbol[0], symbol[1]),
        )


def test_a_wrong_ssid_raises_an_alert_with_who_it_probably_is(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-5", ("/", "["), n=4)     # a person
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    alert = next(a for a in data["ssid_alerts"] if a["station_key"] == "WX0MIK-5")
    assert alert["packets"] == 4
    assert alert["looks_like_infrastructure"] is False
    assert alert["symbol"] == "Person / jogger"
    # It offers the roster entry it probably belongs to.
    assert [c["display_label"] for c in alert["roster_candidates"]] == ["Aid 3"]


def test_a_digipeater_is_flagged_as_equipment(setup):
    """The symbol is the reliable clue: /# is a digipeater, not a person."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-7", ("/", "#"))
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    alert = next(a for a in data["ssid_alerts"] if a["station_key"] == "WX0MIK-7")
    assert alert["looks_like_infrastructure"] is True
    assert alert["symbol"] == "Digipeater"


def test_adopting_moves_the_roster_entry_and_clears_the_alert(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-5", ("/", "["))
    conn.close()

    with TestClient(app) as client:
        response = client.post(
            f"/api/m2026/{tokens['ncs']}/ssid/adopt",
            json={"from_station_key": "WX0MIK-1", "to_station_key": "WX0MIK-5"},
        )
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert response.status_code == 200
    assert response.json()["display_label"] == "Aid 3"
    # The label survived the identity change, and the alert is gone.
    entry = next(r for r in data["roster"] if r["station_key"] == "WX0MIK-5")
    assert entry["display_label"] == "Aid 3"
    assert data["ssid_alerts"] == []


def test_ignoring_clears_the_alert(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-7", ("/", "#"))
    conn.close()

    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/ssid/ignore",
                    json={"station_key": "WX0MIK-7", "reason": "digipeater"})
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert data["ssid_alerts"] == []


def test_adopting_across_callsigns_is_refused(setup):
    """Guards a mis-click that would silently reassign someone else's identity."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    conn.close()

    with TestClient(app) as client:
        response = client.post(
            f"/api/m2026/{tokens['ncs']}/ssid/adopt",
            json={"from_station_key": "WX0MIK-1", "to_station_key": "N0CALL-5"},
        )
    assert response.status_code == 400
    assert "different callsign" in response.json()["detail"]


def test_read_only_roles_see_alerts_but_cannot_resolve_them(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-5", ("/", "["))
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['liaison']}/state").json()
        blocked = client.post(f"/api/m2026/{tokens['liaison']}/ssid/ignore",
                              json={"station_key": "WX0MIK-5"})

    assert len(data["ssid_alerts"]) == 1
    assert blocked.status_code == 403


def test_a_correctly_rostered_station_raises_no_alert(setup):
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    _heard(conn, event_id, "N0CALL-7", ("/", ">"))
    conn.close()

    with TestClient(app) as client:
        data = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert data["ssid_alerts"] == []


def test_ignoring_hides_positions_already_stored(setup):
    """"Ignore" must take it off the map, not merely stop future packets -
    otherwise the digipeater sits there after being dismissed."""
    app, tokens, db_path, event_id = setup
    conn = db.connect(db_path)
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    _heard(conn, event_id, "WX0MIK-7", ("/", "#"), n=3)
    conn.close()

    with TestClient(app) as client:
        before = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
        assert any(p["station_key"] == "WX0MIK-7" for p in before["positions"])

        client.post(f"/api/m2026/{tokens['ncs']}/ssid/ignore",
                    json={"station_key": "WX0MIK-7"})
        after = client.get(f"/api/m2026/{tokens['ncs']}/state").json()

    assert not any(p["station_key"] == "WX0MIK-7" for p in after["positions"])
    assert after["ssid_alerts"] == []
