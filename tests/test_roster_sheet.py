"""The printed roster: who is where, on paper, for the briefing table.

Callsign, operator, the place they are posted at, its coordinates and its
What3Words address - the five things a net control binder or a public
safety liaison needs when the screen is not an option. Built by the server
so the order is the club's own place order (the one the Places tab sets),
and printed by the browser, whose "Save as PDF" is the PDF: no library to
install on a club laptop.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, categories, db, users, web
from courseops.config import Settings
from courseops.web import create_app


def _place(conn, event_id, name, poi_type, lat, lon, order, w3w=None):
    cur = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order,"
        " what3words) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, name, poi_type, lat, lon, order, w3w))
    return cur.lastrowid


@pytest.fixture
def event(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               event_date="2026-10-17")
    water = categories.add_poi_category(conn, event_id, "Water stops",
                                        staffed=True)["key"]
    # Named so that name order and course order disagree.
    zulu = _place(conn, event_id, "Zulu", "aid_station", 44.1, -94.0, 1,
                  "filled.count.soap")
    alpha = _place(conn, event_id, "Alpha", "aid_station", 44.2, -94.1, 3)
    water_a = _place(conn, event_id, "A", water, 44.123456, -93.987654, 2,
                     "index.home.raft")
    for key, label, op, poi in [
        ("KD0AAA", "Late post", "Ann", alpha),
        ("KD0BBB", "Early post", "Bea", zulu),
        ("KD0CCC", "Water", "Cal", water_a),
        ("KD0ZZZ", "Sweep", "Zed", None),
        ("KD0DDD", "SAG 1", None, None),
    ]:
        db.upsert_roster_entry(conn, event_id, key, label, "rover",
                               operator_name=op)
        if poi:
            db.assign_station_to_poi(conn, event_id, key, poi)
    return conn, db_path, event_id


def test_posted_stations_come_in_the_clubs_place_order(event):
    """Course order, not callsign or name order: the sheet is read top to
    bottom along the course."""
    conn, _, event_id = event
    rows = admin.roster_sheet(conn, event_id)["rows"]
    assert [r["callsign"] for r in rows[:3]] == ["KD0BBB", "KD0CCC", "KD0AAA"]


def test_the_unposted_follow_with_no_position(event):
    """A sweep or a SAG moves all day, so it has no fixed position to print -
    a blank, never the coordinates of somewhere they were once."""
    conn, _, event_id = event
    rows = admin.roster_sheet(conn, event_id)["rows"]
    tail = rows[3:]
    assert [r["callsign"] for r in tail] == ["KD0DDD", "KD0ZZZ"]
    for row in tail:
        assert row["posted_at"] is None
        assert row["coordinates"] is None
        assert row["what3words"] is None


def test_a_row_carries_the_five_columns(event):
    conn, _, event_id = event
    row = next(r for r in admin.roster_sheet(conn, event_id)["rows"]
               if r["callsign"] == "KD0CCC")
    assert row == {
        "callsign": "KD0CCC",
        "operator": "Cal",
        # The layer, singularised, in front of a name that does not say it.
        "posted_at": "Water stop A",
        # Five decimals is about a metre; more is noise on paper.
        "coordinates": "44.12346, -93.98765",
        "what3words": "index.home.raft",
    }


def test_a_missing_operator_is_blank_not_none(event):
    conn, _, event_id = event
    row = next(r for r in admin.roster_sheet(conn, event_id)["rows"]
               if r["callsign"] == "KD0DDD")
    assert row["operator"] == ""


def test_the_heading_names_the_event_and_its_date(event):
    conn, _, event_id = event
    sheet = admin.roster_sheet(conn, event_id)
    assert (sheet["event_name"], sheet["event_date"]) == (
        "Spring Marathon", "2026-10-17")


def test_the_heard_ssid_is_the_callsign_printed(event):
    """A bare callsign on the roster is heard under an SSID; the sheet says
    which, because that is what a scanner or aprs.fi shows."""
    conn, _, event_id = event
    conn.execute("UPDATE roster SET bound_key = 'KD0AAA-7'"
                 " WHERE station_key = 'KD0AAA'")
    callsigns = [r["callsign"] for r in admin.roster_sheet(conn, event_id)["rows"]]
    assert "KD0AAA-7" in callsigns and "KD0AAA" not in callsigns


@pytest.mark.parametrize("layer, name, expected", [
    ("Water stops", "A", "Water stop A"),
    ("Aid station", "Aid station 4", "Aid station 4"),   # already says it
    ("Access", "North", "Access North"),                  # never "Acces"
    ("Start / finish", "Start / finish", "Start / finish"),
    ("", "Mile 13", "Mile 13"),
])
def test_place_names_follow_the_map_pages_rule(layer, name, expected):
    """The same words the live map uses (`placeName` in app.js), so the
    sheet and the screen name a place the same way."""
    assert categories.place_name(layer, name) == expected


# --- the endpoint -------------------------------------------------------------

def test_setup_serves_it_to_a_signed_in_admin_only(event):
    conn, db_path, event_id = event
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    url = f"/api/setup/events/{event_id}/roster/sheet"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        client.post("/api/setup/login",
                    json={"username": "mike", "password": "a-long-enough-password"})
        response = client.get(url)
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 5


# --- the client ---------------------------------------------------------------

SETUP_HTML = (web.STATIC_DIR / "setup.html").read_text(encoding="utf-8")
SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")
SETUP_CSS = (web.STATIC_DIR / "setup.css").read_text(encoding="utf-8")


def test_the_roster_tab_has_a_print_button_and_a_sheet_to_print():
    assert 'id="roster-print"' in SETUP_HTML
    assert 'id="roster-sheet"' in SETUP_HTML
    assert "body.print-roster" in SETUP_CSS


def test_every_value_on_the_sheet_is_escaped():
    render = SETUP_JS[SETUP_JS.index("function renderRosterSheet"):]
    render = render[:render.index("\n}\n")]
    for field in ("callsign", "operator", "posted_at", "coordinates",
                  "what3words", "event_name"):
        assert f"esc(" in render and field in render, field
    assert "${r." not in render.replace("${esc(r.", "")
