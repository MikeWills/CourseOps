"""Event notes: freeform, location-less, for the organizer afterwards.

"Bring more pizza next year" is not a pickup and not a place on the course,
so it is neither an incident nor a pin. Anyone holding any link may add one -
every volunteer's day is a different view of the event - and the note is what
the club forgets by the following spring.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import access, db, event_notes, importer, report
from courseops.config import Settings
from courseops.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               timezone="America/Chicago")
    return conn, event_id


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               center_lat=34.7, center_lon=-86.5)
    importer.stage_file(conn, event_id, FIXTURE)
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    tokens = access.ensure_tokens(conn, event_id)
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    return create_app(settings), tokens, event_id


def notes_url(token: str) -> str:
    return f"/api/m2026/{token}/event-notes"


# --- the module -------------------------------------------------------------

def test_a_note_is_text_and_a_time_and_nothing_else(event):
    conn, event_id = event
    row = event_notes.create(conn, event_id, "  Bring more pizza next year ", by="MW")
    assert row["text"] == "Bring more pizza next year"
    assert row["created_by"] == "MW"
    assert row["created_at"]
    data = event_notes.EventNote(row).as_dict()
    assert set(data) >= {"id", "text", "created_at", "created_by"}
    assert "lat" not in data and "status" not in data


def test_an_empty_note_is_refused(event):
    conn, event_id = event
    with pytest.raises(event_notes.EventNoteError):
        event_notes.create(conn, event_id, "   ")
    assert event_notes.for_event(conn, event_id) == []


def test_text_is_capped_not_rejected(event):
    conn, event_id = event
    row = event_notes.create(conn, event_id, "x" * 2000)
    assert len(row["text"]) == event_notes.MAX_TEXT_LENGTH


def test_notes_list_newest_first_and_are_event_scoped(event):
    conn, event_id = event
    other = db.create_event(conn, "other", "Other")
    first = event_notes.create(conn, event_id, "first")
    second = event_notes.create(conn, event_id, "second")
    event_notes.create(conn, other, "elsewhere")
    conn.execute("UPDATE event_note SET created_at = '2026-10-17T14:00:00Z' WHERE id = ?",
                 (first["id"],))
    conn.execute("UPDATE event_note SET created_at = '2026-10-17T15:00:00Z' WHERE id = ?",
                 (second["id"],))
    assert [r["text"] for r in event_notes.for_event(conn, event_id)] == ["second", "first"]


def test_update_and_delete_are_scoped_to_the_event(event):
    conn, event_id = event
    other = db.create_event(conn, "other", "Other")
    row = event_notes.create(conn, event_id, "typo")
    with pytest.raises(event_notes.EventNoteError):
        event_notes.update(conn, other, row["id"], "fixed")
    with pytest.raises(event_notes.EventNoteError):
        event_notes.delete(conn, other, row["id"])
    assert event_notes.update(conn, event_id, row["id"], "fixed")["text"] == "fixed"
    gone = event_notes.delete(conn, event_id, row["id"])
    assert gone["id"] == row["id"]
    assert event_notes.for_event(conn, event_id) == []


def test_a_deleted_event_takes_its_notes_with_it(event):
    conn, event_id = event
    event_notes.create(conn, event_id, "orphan?")
    conn.execute("DELETE FROM event WHERE id = ?", (event_id,))
    assert conn.execute("SELECT COUNT(*) FROM event_note").fetchone()[0] == 0


# --- access -----------------------------------------------------------------

def test_every_role_may_add_an_event_note_staff_included():
    """Everyone's day is a different view of the event, and the forwarded
    link is held by the people with the least other way to say so."""
    for role in access.ROLES:
        grant = access.Access(1, "m2026", role, "t")
        assert grant.can(access.CAP_EVENT_NOTE), role


def test_only_the_roles_running_the_event_read_the_list():
    """Everyone writes into it; NCS, Liaison and Logistics read it. SAG is in
    a vehicle working the queue, and Staff is the link that travels."""
    assert {role for role in access.ROLES
            if access.Access(1, "m2026", role, "t").can(access.CAP_EVENT_NOTE_VIEW)} == {
        access.ROLE_NCS, access.ROLE_LIAISON, access.ROLE_LOGISTICS}


def test_only_ncs_and_sag_may_delete_an_event_note():
    assert {role for role in access.ROLES
            if access.Access(1, "m2026", role, "t").can(access.CAP_INCIDENTS)} == {
        access.ROLE_NCS, access.ROLE_SAG}


def test_staff_may_add_a_note_and_still_nothing_else(app):
    """The one write the forwarded link has. The pickup queue, the roster
    and the nearby list stay exactly as closed as before."""
    app, tokens, _ = app
    with TestClient(app) as client:
        created = client.post(notes_url(tokens["staff"]),
                              json={"text": "More pizza", "changed_by": "vol"})
        assert created.status_code == 201, created.text
        assert created.json()["text"] == "More pizza"
        state = client.get(f"/api/m2026/{tokens['staff']}/state").json()
        # Written, never read back: not sent empty, not sent at all.
        assert "event_notes" not in state
        assert state["capabilities"] == [access.CAP_EVENT_NOTE]
        assert "incidents" not in state and "nearby" not in state
        refused = [
            client.post(f"/api/m2026/{tokens['staff']}/incidents",
                        json={"lat": 44.1, "lon": -94.0}),
            client.post(f"{notes_url(tokens['staff'])}/{created.json()['id']}/delete"),
        ]
    assert [r.status_code for r in refused] == [403, 403]


def test_the_list_reaches_the_reading_roles_and_no_others(app):
    app, tokens, _ = app
    with TestClient(app) as client:
        client.post(notes_url(tokens["sag"]), json={"text": "Cones short at 5th"})
        for role in ("ncs", "liaison", "logistics"):
            state = client.get(f"/api/m2026/{tokens[role]}/state").json()
            assert [n["text"] for n in state["event_notes"]] == ["Cones short at 5th"], role
        for role in ("sag", "staff"):
            assert "event_notes" not in client.get(
                f"/api/m2026/{tokens[role]}/state").json(), role


def test_empty_or_missing_text_is_400(app):
    app, tokens, _ = app
    with TestClient(app) as client:
        assert client.post(notes_url(tokens["ncs"]), json={}).status_code == 400
        assert client.post(notes_url(tokens["ncs"]), json={"text": " "}).status_code == 400
        assert client.post(notes_url(tokens["ncs"]),
                           json={"text": ["a"]}).status_code == 400


def test_the_response_is_the_broadcast_and_the_reading_roles_hear_it(app):
    """One payload: what the route answers with is what the socket carries.
    The writer gets its row back in the response even when its own socket
    is never sent it."""
    app, tokens, _ = app
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['liaison']}") as ws:
            created = client.post(notes_url(tokens["sag"]),
                                  json={"text": "Van 2 needs a spare tyre"}).json()
            heard = ws.receive_json()
            assert heard["type"] == "event_note" and heard["change"] == "created"
            assert {k: v for k, v in heard.items() if k not in ("type", "change")} == created

            edited = client.post(f"{notes_url(tokens['liaison'])}/{created['id']}",
                                 json={"text": "Van 2 needs TWO spare tyres"}).json()
            heard = ws.receive_json()
            assert heard["change"] == "edited"
            assert {k: v for k, v in heard.items() if k not in ("type", "change")} == edited

            client.post(f"{notes_url(tokens['ncs'])}/{created['id']}/delete")
            heard = ws.receive_json()
            assert heard["change"] == "deleted" and heard["id"] == created["id"]
        assert client.get(f"/api/m2026/{tokens['ncs']}/state").json()["event_notes"] == []


def test_a_socket_that_cannot_read_the_list_is_not_handed_entries(app):
    """Staff and SAG add and never hear: the first thing their socket
    carries after a note is the next thing they ARE told."""
    app, tokens, _ = app
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/m2026/{tokens['staff']}") as ws:
            client.post(notes_url(tokens["staff"]), json={"text": "quiet"})
            client.post(notes_url(tokens["ncs"]), json={"text": "quiet too"})
            client.post(f"/api/m2026/{tokens['ncs']}/station/N0CALL-7/status",
                        json={"op_status": "active"})
            message = ws.receive_json()
            while message["type"] == "heartbeat":
                message = ws.receive_json()
            assert message["type"] == "station_status"


def test_a_note_from_another_event_is_404(app):
    app, tokens, _ = app
    with TestClient(app) as client:
        assert client.post(f"{notes_url(tokens['ncs'])}/999",
                           json={"text": "x"}).status_code == 404
        assert client.post(f"{notes_url(tokens['ncs'])}/999/delete").status_code == 404


# --- the report ---------------------------------------------------------------

def test_the_report_carries_event_notes_with_times_and_no_names(event):
    conn, event_id = event
    row = event_notes.create(conn, event_id, "Bring more pizza", by="WX0MIK")
    conn.execute("UPDATE event_note SET created_at = '2026-10-17T14:32:00Z' WHERE id = ?",
                 (row["id"],))
    data = report.build(conn, event_id)
    assert [(n.at, n.text) for n in data.event_notes] == [
        ("2026-10-17T14:32:00Z", "Bring more pizza")]
    html = report.render(data)
    assert "Event notes" in html
    assert "Bring more pizza" in html
    assert "WX0MIK" not in html


def test_the_report_says_so_when_there_are_no_event_notes(event):
    conn, event_id = event
    html = report.render(report.build(conn, event_id))
    assert "Event notes" in html
