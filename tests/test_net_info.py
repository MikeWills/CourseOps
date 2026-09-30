"""Net info: the frequencies and other standing notes for the net.

"Race RX 147.330 (136.5) TX 147.930 (136.5)" is set once in setup and read
all day by everyone on the net, so it is a property of the event - free
text, line breaks kept - sent to every role in the snapshot, the forwarded
Staff link included. Repeater frequencies are published anyway; nothing
here needs hiding, and a volunteer who cannot find the backup frequency is
the failure this exists to prevent.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, users, web
from courseops.config import Settings
from courseops.web import create_app

NET = ("Race RX 147.330 (136.5) TX 147.930 (136.5)\n"
       "Logistics RX 443.650 (114.8) TX 448.650 (114.8)\n"
       "Note: Backup Frequency RX 147.240 (136.5) TX 147.840 (136.5)")


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    return conn, event_id


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               center_lat=34.7, center_lon=-86.5)
    tokens = access.ensure_tokens(conn, event_id)
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    return create_app(settings), tokens, event_id


def _sign_in(client):
    client.post("/api/setup/login",
                json={"username": "mike", "password": "a-long-enough-password"})


# --- storing it ---------------------------------------------------------------

def test_net_info_keeps_its_lines(event):
    """The lines are the structure: one channel a line is how it is written
    on the briefing sheet and how it has to read on a phone."""
    conn, event_id = event
    saved = admin.update_event(conn, event_id, {"net_info": f"  {NET}\n\n"})
    assert saved["net_info"] == NET


def test_windows_line_endings_arrive_as_plain_newlines(event):
    conn, event_id = event
    saved = admin.update_event(conn, event_id, {"net_info": "A\r\nB"})
    assert saved["net_info"] == "A\nB"


def test_emptying_the_box_clears_it(event):
    """A blank box is "there is none", so the section disappears from every
    phone rather than showing an empty heading."""
    conn, event_id = event
    admin.update_event(conn, event_id, {"net_info": NET})
    saved = admin.update_event(conn, event_id, {"net_info": "   "})
    assert saved["net_info"] is None


def test_too_long_is_refused_not_cut_short(event):
    """Truncating silently would drop the LAST line - which is where the
    backup frequency usually goes."""
    conn, event_id = event
    with pytest.raises(ValueError, match=str(admin.NET_INFO_MAX)):
        admin.update_event(conn, event_id,
                           {"net_info": "x" * (admin.NET_INFO_MAX + 1)})
    admin.update_event(conn, event_id, {"net_info": "x" * admin.NET_INFO_MAX})


def test_a_new_event_can_carry_it_from_the_start(event):
    conn, _ = event
    org = users.create_organization(conn, "club", "Club")
    created = admin.create_event(
        conn, {"slug": "bike", "name": "Bike", "net_info": NET}, org["id"])
    assert created["net_info"] == NET


def test_an_existing_database_gets_the_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS skips a table that is already there."""
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    conn.execute("ALTER TABLE event DROP COLUMN net_info")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute("PRAGMA table_info(event)")}
    assert "net_info" in names


# --- serving it ---------------------------------------------------------------

def test_every_role_is_sent_it(app):
    """Everyone on the net needs the frequencies - Staff, the forwarded
    link that can do nothing else, included."""
    application, tokens, event_id = app
    with TestClient(application) as client:
        _sign_in(client)
        assert client.post(f"/api/setup/events/{event_id}",
                           json={"net_info": NET}).status_code == 200
        for role in access.ROLES:
            state = client.get(f"/api/m2026/{tokens[role]}/state").json()
            assert state["event"]["net_info"] == NET, role


def test_saving_it_reaches_phones_already_open(app):
    """Changed on race morning - the backup repeater came up - it has to
    arrive without anyone reloading."""
    application, tokens, event_id = app
    with TestClient(application) as client:
        _sign_in(client)
        with client.websocket_connect(f"/ws/m2026/{tokens['staff']}") as ws:
            client.post(f"/api/setup/events/{event_id}", json={"net_info": NET})
            assert ws.receive_json()["type"] == "resync"


def test_a_list_is_a_message_not_a_traceback(app):
    application, _, event_id = app
    with TestClient(application) as client:
        _sign_in(client)
        response = client.post(f"/api/setup/events/{event_id}",
                               json={"net_info": ["x"]})
    assert response.status_code == 400


# --- the clients --------------------------------------------------------------

INDEX = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
SETUP_HTML = (web.STATIC_DIR / "setup.html").read_text(encoding="utf-8")
SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")


def test_the_map_page_has_a_net_info_section_above_the_lead_runners():
    """Read all day, so it goes near the top - above everything but the
    alerts - not down among the layer switches."""
    assert 'id="net-info-section"' in INDEX
    assert INDEX.index('id="net-info-section"') < INDEX.index('id="leader-section"')


def test_on_a_phone_it_stays_above_the_lifted_role_sections():
    """A phone lifts the role's own sections to the top of the sheet - for
    Staff and Logistics that is seventeen stations - and Net info went
    below all of them. Three lines, folded to one heading if unwanted, so
    it rides with the alerts instead."""
    order = APP_JS[APP_JS.index("function desiredSheetOrder"):]
    order = order[:order.index("\n}\n")]
    assert "'ssid-section', 'net-info-section'" in order


def test_net_info_is_drawn_as_text_never_as_markup():
    """It is typed by a person and shown on every phone; textContent is
    the escaping, and pre-wrap is what keeps the lines."""
    render = APP_JS[APP_JS.index("function renderNetInfo"):]
    render = render[:render.index("\n}\n")]
    assert "textContent" in render
    assert "innerHTML" not in render


def test_setup_edits_it_on_the_event_form():
    assert 'id="ev-net-info"' in SETUP_HTML
    submit = SETUP_JS[SETUP_JS.index("$('event-form').addEventListener('submit'"):]
    submit = submit[:submit.index("\n});\n")]
    # Both branches: editing an event, and creating one.
    assert submit.count("net_info:") == 2
