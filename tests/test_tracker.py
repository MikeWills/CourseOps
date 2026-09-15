"""Phone tracking (issue #6): a tracking app on a non-ham's phone posting
positions under a designator, with the roster as the allowlist."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from courseops import access, db, tracker
from courseops.config import Settings
from courseops.web import create_app


@pytest.fixture
def setup(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               center_lat=34.7, center_lon=-86.5)
    db.upsert_roster_entry(conn, event_id, "M1", "Medic 1", "rover",
                           tracked_by=db.TRACKED_BY_PHONE)
    db.upsert_roster_entry(conn, event_id, "BIKE2", "Bike medic 2", "rover",
                           tracked_by=db.TRACKED_BY_PHONE)
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    token = access.generate_token()
    db.set_tracker_token(conn, event_id, token)
    links = access.ensure_tokens(conn, event_id)
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    return create_app(settings), token, links, db_path, event_id


# --- designators -------------------------------------------------------------

@pytest.mark.parametrize("typed", ["m1", "M-1", "m_1", " M 1 ", "M--1"])
def test_five_spellings_are_one_designator(typed):
    """Medic1, medic 1, Medic-1 and Medic_1 are one person; a mismatch makes
    them invisible while their phone transmits happily."""
    assert tracker.normalise_designator(typed) == "M1"


def test_a_designator_is_capped_and_never_none():
    assert tracker.normalise_designator(None) == ""
    assert len(tracker.normalise_designator("x" * 40)) == tracker.DESIGNATOR_MAX


# --- timestamps --------------------------------------------------------------

NOW = datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [
    1788268800, "1788268800", 1788268800000,          # epoch s, s as text, ms
    "2026-09-01T13:20:00Z", "2026-09-01T13:20:00+00:00",
    "2026-09-01 13:20:00",                            # Traccar's other shape
])
def test_every_timestamp_shape_lands_on_the_reported_time(value):
    assert tracker.reported_at(value, NOW) == "2026-09-01T13:20:00Z"


def test_a_future_timestamp_is_clamped_to_now():
    """A phone with a wrong clock must not produce a fix that stays fresh
    for an hour."""
    later = NOW + timedelta(hours=1)
    assert tracker.reported_at(int(later.timestamp()), NOW) == "2026-09-01T14:00:00Z"


def test_a_missing_or_unreadable_timestamp_falls_back_to_now():
    assert tracker.reported_at(None, NOW) == "2026-09-01T14:00:00Z"
    assert tracker.reported_at("yesterday", NOW) == "2026-09-01T14:00:00Z"


# --- payloads ----------------------------------------------------------------

def test_osmand_speed_is_knots_and_stored_as_kmh():
    designator, report = tracker.parse_osmand(
        {"id": "Medic-1", "lat": "34.7", "lon": "-86.5", "speed": "10",
         "bearing": "270", "accuracy": "12", "batt": "75"})
    assert designator == "MEDIC1"
    assert report.speed_kmh == pytest.approx(18.52)
    assert report.course_deg == 270
    assert report.comment == "accuracy 12 m, battery 75%"
    assert report.aprs_format == tracker.FORMAT


def test_owntracks_uses_tid_and_falls_back_to_the_topic():
    designator, _ = tracker.parse_owntracks(json.dumps(
        {"_type": "location", "tid": "M1", "lat": 34.7, "lon": -86.5,
         "tst": 1788268800}))
    assert designator == "M1"
    designator, _ = tracker.parse_owntracks(json.dumps(
        {"_type": "location", "lat": 34.7, "lon": -86.5,
         "topic": "owntracks/jane/BIKE-2"}))
    assert designator == "BIKE2"


def test_owntracks_non_location_messages_are_ignored_not_rejected():
    """The app also posts lwt, transition and waypoint messages; an app that
    gets an error for one may stop sending locations too."""
    assert tracker.parse_owntracks(json.dumps({"_type": "lwt", "tst": 1})) is None


@pytest.mark.parametrize("params", [
    {"id": "M1", "lat": "0", "lon": "0"},          # GPS not locked
    {"id": "M1", "lat": "91", "lon": "0"},
    {"id": "M1", "lon": "-86.5"},
    {"lat": "34.7", "lon": "-86.5"},                # nobody
])
def test_an_unusable_report_is_rejected(params):
    with pytest.raises(tracker.Rejected):
        tracker.parse_osmand(params)


def test_the_qr_carries_an_owntracks_configuration_for_the_url():
    link = tracker.owntracks_link("https://x.example/track/m2026/tok")
    assert link.startswith("owntracks:///config?inline=")
    config = json.loads(base64.b64decode(link.split("inline=", 1)[1]))
    assert config["_type"] == "configuration"
    assert config["mode"] == 3
    assert config["url"] == "https://x.example/track/m2026/tok"
    assert "tid" not in config, "the person sets their own designator"
    svg = tracker.qr_svg(link)
    assert svg.startswith("<svg")


# --- the endpoint -------------------------------------------------------------

def test_a_known_designator_is_stored_under_the_roster_key(setup):
    app, token, _, db_path, event_id = setup
    with TestClient(app) as client:
        response = client.get(
            f"/track/m2026/{token}",
            params={"id": "m-1", "lat": "34.71",
                    "lon": "-86.51", "timestamp": "1788268800"})
        assert response.status_code == 200
        assert response.json() == []
    conn = db.connect(db_path)
    rows = db.latest_position_per_station(conn, event_id)
    assert [(r["station_key"], r["received_at"]) for r in rows] == [
        ("M1", "2026-09-01T13:20:00Z")]
    assert rows[0]["aprs_format"] == tracker.FORMAT


def test_owntracks_posts_json_and_traccar_may_post_a_form(setup):
    app, token, _, db_path, event_id = setup
    with TestClient(app) as client:
        assert client.post(
            f"/track/m2026/{token}",
            json={"_type": "location", "tid": "M1", "lat": 34.71,
                  "lon": -86.51, "tst": 1788268800, "batt": 80},
        ).status_code == 200
        assert client.post(
            f"/track/m2026/{token}",
            data={"id": "bike 2", "lat": "34.72", "lon": "-86.52"},
        ).status_code == 200
    conn = db.connect(db_path)
    keys = sorted(r["station_key"] for r in db.latest_position_per_station(conn, event_id))
    assert keys == ["BIKE2", "M1"]


def test_a_wrong_or_missing_token_is_404_never_403(setup):
    """403 would confirm the event exists. And with no token at all - phone
    tracking off - the endpoint does not exist either."""
    app, token, _, db_path, event_id = setup
    fix = {"id": "M1", "lat": "34.71", "lon": "-86.51"}
    with TestClient(app) as client:
        assert client.get("/track/m2026/not-it", params=fix).status_code == 404
        assert client.get("/track/other/" + token, params=fix).status_code == 404
        conn = db.connect(db_path)
        db.set_tracker_token(conn, event_id, None)
        conn.close()
        assert client.get(f"/track/m2026/{token}", params=fix).status_code == 404


def test_an_unknown_designator_is_held_for_ncs_and_never_stored(setup):
    """The roster is the allowlist. An unrecognised designator is neither
    silently dropped nor silently accepted: it goes to the nearby list, where
    NCS can match it - which is also how a typo on the phone is recovered."""
    app, token, links, db_path, event_id = setup
    with TestClient(app) as client:
        client.get(f"/track/m2026/{token}",
                   params={"id": "MEDIC1", "lat": "34.71", "lon": "-86.51"})
        conn = db.connect(db_path)
        assert db.latest_position_per_station(conn, event_id) == []
        conn.close()
        state = client.get(f"/api/m2026/{links['ncs']}/state").json()
        assert [n["station_key"] for n in state["nearby"]] == ["MEDIC1"]
        assert state["nearby"][0]["symbol"] == "Phone"
        # Staff never sees who is nearby.
        assert "nearby" not in client.get(f"/api/m2026/{links['staff']}/state").json()

        # NCS says "MEDIC1 is Medic 1". From then on it is stored under M1's
        # tracking key and joins the roster row.
        matched = client.post(f"/api/m2026/{links['ncs']}/ssid/adopt",
                              json={"from_station_key": "M1",
                                    "to_station_key": "MEDIC1"})
        assert matched.status_code == 200, matched.text
        client.get(f"/track/m2026/{token}",
                   params={"id": "medic1", "lat": "34.72", "lon": "-86.52"})
        conn = db.connect(db_path)
        rows = db.latest_position_per_station(conn, event_id)
        assert [r["station_key"] for r in rows] == ["MEDIC1"]
        entry = conn.execute("SELECT * FROM roster WHERE station_key = 'M1'").fetchone()
        assert db.tracking_key(entry) == "MEDIC1"


def test_a_buffered_backlog_keeps_the_newest_fix_on_top(setup):
    """A phone that buffered through a dead zone delivers ten minutes of
    fixes in one burst, in whatever order it kept them. The newest by
    REPORTED time is the position, never the last one inserted; a resent
    duplicate is not a second row."""
    app, token, _, db_path, event_id = setup
    with TestClient(app) as client:
        for stamp, lat in [(1788268800, "34.71"), (1788268200, "34.70"),
                           (1788268800, "34.71")]:
            client.get(f"/track/m2026/{token}",
                       params={"id": "M1", "lat": lat, "lon": "-86.5",
                               "timestamp": str(stamp)})
    conn = db.connect(db_path)
    rows = conn.execute("SELECT received_at FROM position ORDER BY id").fetchall()
    assert [r["received_at"] for r in rows] == [
        "2026-09-01T13:20:00Z", "2026-09-01T13:10:00Z"]
    latest = db.latest_position_per_station(conn, event_id)
    assert latest[0]["received_at"] == "2026-09-01T13:20:00Z"
    assert latest[0]["lat"] == 34.71


def test_a_phone_designator_never_enters_the_aprs_filter(setup):
    _, _, _, db_path, event_id = setup
    conn = db.connect(db_path)
    assert db.tracked_station_keys(conn, event_id) == ["N0CALL-7"]
    assert db.rostered_base_callsigns(conn, event_id) == {"N0CALL"}
    # ...but a phone entry is still one of ours for every other purpose.
    assert "M1" in db.all_station_keys(conn, event_id)


# --- setup -----------------------------------------------------------------

def _sign_in(client, db_path):
    from courseops import users
    conn = db.connect(db_path)
    users.create_user(conn, "chief", "correct horse battery", role=users.ROLE_SYSTEM_ADMIN)
    conn.close()
    response = client.post("/api/setup/login",
                           json={"username": "chief",
                                 "password": "correct horse battery"})
    assert response.status_code == 200, response.text


def test_setup_switches_phone_tracking_on_and_prints_the_card(setup):
    app, _, _, db_path, event_id = setup
    with TestClient(app) as client:
        _sign_in(client, db_path)
        origin = {"Origin": "http://testserver"}
        # The fixture already made a token; turn it off and back on.
        off = client.post(f"/api/setup/events/{event_id}/tracking/phone",
                          json={"action": "off"}, headers=origin).json()
        assert off["phone"]["enabled"] is False
        assert "url" not in off["phone"]
        on = client.post(f"/api/setup/events/{event_id}/tracking/phone",
                         json={"action": "on"}, headers=origin).json()["phone"]
        assert on["enabled"] is True
        assert on["url"].startswith("http://testserver/track/m2026/")
        assert on["qr_svg"].startswith("<svg")
        assert [d["station_key"] for d in on["designators"]] == ["BIKE2", "M1"]
        # "on" again keeps the URL on the printed card.
        again = client.post(f"/api/setup/events/{event_id}/tracking/phone",
                            json={"action": "on"}, headers=origin).json()["phone"]
        assert again["url"] == on["url"]
        reset = client.post(f"/api/setup/events/{event_id}/tracking/phone",
                            json={"action": "reset"}, headers=origin).json()["phone"]
        assert reset["url"] != on["url"]
        assert client.get(f"/api/setup/events/{event_id}/tracking").json()["phone"] == reset


def test_the_roster_accepts_a_designator_for_a_phone_entry_only(setup):
    from courseops import admin
    _, _, _, db_path, event_id = setup
    conn = db.connect(db_path)
    saved = admin.save_roster_entry(conn, event_id, {
        "station_key": "medic-3", "display_label": "Medic 3",
        "tracked_by": "phone"})
    assert saved["station_key"] == "MEDIC3"
    assert saved["tracked_by"] == "phone"
    with pytest.raises(ValueError, match="callsign"):
        admin.save_roster_entry(conn, event_id, {
            "station_key": "bike 2", "display_label": "Bike 2"})
    with pytest.raises(ValueError, match="designator"):
        admin.save_roster_entry(conn, event_id, {
            "station_key": "m.3", "display_label": "Medic 3",
            "tracked_by": "phone"})
