"""Archiving a finished event, and taking a copy of one off the server (#4).

The feed records where volunteers are, and after an event nothing needs
that any more - so archiving is what ends it: tracking off, positions gone,
links dead, the event out of the list. The report, the incidents, the notes
and the roster are the event's record and stay. Unarchive reopens it.

The export is a SQLite file in the same schema holding one event, readable
by running the app against it. Nothing install-scoped travels: no accounts,
no sessions, no role links - and no positions, for the same reason archiving
deletes them.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from courseops import access, admin, db, event_notes, feed, incidents, users, web
from courseops.config import Settings
from courseops.parser import parse_packet

PACKET = "N0CALL-7>APDR16,TCPIP*,qAC,T2:=4410.00N/09400.00W["


@pytest.fixture
def world(tmp_path, monkeypatch):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")["id"]
    event_id = db.create_event(conn, "bike", "Bike Fall", organization_id=org,
                               center_lat=44.1, center_lon=-94.0)
    db.create_event(conn, "other", "Other", organization_id=org)
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Sweep", "sweep")
    db.insert_position(conn, event_id, parse_packet(PACKET))
    incidents.create(conn, event_id, 44.1, -94.0, bib="212", note="cramp")
    event_notes.create(conn, event_id, "Bring more pizza")
    tokens = access.ensure_tokens(conn, event_id)
    db.set_tracker_token(conn, event_id, access.generate_token())
    users.create_user(conn, "mike", "a-long-enough-password",
                      users.ROLE_SYSTEM_ADMIN)
    conn.close()

    running: set[str] = set()

    async def fake_feed(settings, slug, on_position=None, max_packets=None,
                        on_nearby=None):
        running.add(slug)
        try:
            await asyncio.Event().wait()
        finally:
            running.discard(slug)

    monkeypatch.setattr(feed, "run_ingest", fake_feed)
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    app = web.create_app(settings)
    return app, db_path, event_id, tokens, running


def _login(client):
    client.post("/api/setup/login",
                json={"username": "mike", "password": "a-long-enough-password"})


def _count(db_path, sql, *params):
    conn = db.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# --- archive ----------------------------------------------------------------

def test_archiving_ends_tracking_and_removes_positions(world):
    """The whole point of #4: after the event the server stops knowing where
    the volunteers are. Both switches off, and the one stored position per
    station deleted."""
    app, db_path, event_id, _, running = world
    with TestClient(app) as client:
        _login(client)
        assert client.post(f"/api/setup/events/{event_id}/tracking",
                           json={"enabled": True}).status_code == 200
        assert running == {"bike"}

        r = client.post(f"/api/setup/events/{event_id}/archive")
        assert r.status_code == 200, r.text
        assert r.json()["archived_at"]
        assert running == set()

    conn = db.connect(db_path)
    event = conn.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    assert event["ingest_enabled"] == 0
    assert event["tracker_token"] is None
    assert event["archived_at"]
    assert conn.execute("SELECT COUNT(*) FROM position WHERE event_id = ?",
                        (event_id,)).fetchone()[0] == 0
    conn.close()


def test_archiving_keeps_the_record(world):
    """The report, incidents, notes and roster are what the organizer and the
    club keep. Archiving must not take them with the positions."""
    app, db_path, event_id, _, _ = world
    with TestClient(app) as client:
        _login(client)
        client.post(f"/api/setup/events/{event_id}/archive")
        assert client.get(f"/setup/events/{event_id}/report").status_code == 200
    assert _count(db_path, "SELECT COUNT(*) FROM incident WHERE event_id = ?", event_id) == 1
    assert _count(db_path, "SELECT COUNT(*) FROM event_note WHERE event_id = ?", event_id) == 1
    assert _count(db_path, "SELECT COUNT(*) FROM roster WHERE event_id = ?", event_id) == 1


def test_an_archived_events_links_answer_404_and_unarchive_restores_them(world):
    """A link forwarded after the event must show nothing. Unarchiving brings
    back the SAME links: the club may reopen an event by mistake-archived,
    and every volunteer already holds those URLs."""
    app, _, event_id, tokens, _ = world
    with TestClient(app) as client:
        _login(client)
        url = f"/api/bike/{tokens['ncs']}/state"
        assert client.get(url).status_code == 200
        client.post(f"/api/setup/events/{event_id}/archive")
        assert client.get(url).status_code == 404
        assert client.get(f"/e/bike/{tokens['ncs']}").status_code == 404

        r = client.post(f"/api/setup/events/{event_id}/unarchive")
        assert r.status_code == 200, r.text
        assert r.json()["archived_at"] is None
        assert client.get(url).status_code == 200


def test_an_archived_events_socket_is_refused(world):
    app, _, event_id, tokens, _ = world
    with TestClient(app) as client:
        _login(client)
        client.post(f"/api/setup/events/{event_id}/archive")
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/bike/{tokens['ncs']}") as ws:
                ws.receive_json()


def test_tracking_cannot_be_turned_on_for_an_archived_event(world):
    """A feed for an event nobody can view would be recording volunteers for
    no one. Refused, with what to do instead."""
    app, _, event_id, _, running = world
    with TestClient(app) as client:
        _login(client)
        client.post(f"/api/setup/events/{event_id}/archive")
        r = client.post(f"/api/setup/events/{event_id}/tracking",
                        json={"enabled": True})
        assert r.status_code == 400
        assert "Unarchive" in r.json()["detail"]
        r = client.post(f"/api/setup/events/{event_id}/tracking/phone",
                        json={"action": "on"})
        assert r.status_code == 400
        assert running == set()


def test_unarchive_leaves_tracking_off(world):
    """Reopening is for looking, or for fixing a mistake. Whether to track
    again is a separate, deliberate press."""
    app, db_path, event_id, _, running = world
    with TestClient(app) as client:
        _login(client)
        client.post(f"/api/setup/events/{event_id}/tracking", json={"enabled": True})
        client.post(f"/api/setup/events/{event_id}/archive")
        client.post(f"/api/setup/events/{event_id}/unarchive")
    assert running == set()
    assert _count(db_path, "SELECT ingest_enabled FROM event WHERE id = ?", event_id) == 0


def test_the_events_list_says_which_are_archived(world):
    """Hiding is the client's job; the list still carries every event so the
    'Show archived' toggle has something to show."""
    app, _, event_id, _, _ = world
    with TestClient(app) as client:
        _login(client)
        client.post(f"/api/setup/events/{event_id}/archive")
        events = {e["slug"]: e for e in client.get("/api/setup/events").json()["events"]}
    assert events["bike"]["archived_at"]
    assert events["other"]["archived_at"] is None


def test_archiving_needs_the_same_rights_as_deleting(world):
    """It ends every link and hides the event from everyone, so an event-level
    admin cannot do it to the club."""
    app, db_path, event_id, _, _ = world
    conn = db.connect(db_path)
    org = conn.execute("SELECT organization_id FROM event WHERE id = ?",
                       (event_id,)).fetchone()[0]
    user = users.create_user(conn, "ev", "a-long-enough-password",
                             users.ROLE_EVENT_ADMIN, organization_id=org)
    users.set_events(conn, user.id, [event_id])
    conn.close()
    with TestClient(app) as client:
        client.post("/api/setup/login",
                    json={"username": "ev", "password": "a-long-enough-password"})
        assert client.post(f"/api/setup/events/{event_id}/archive").status_code == 403


# --- export -----------------------------------------------------------------

def test_the_export_holds_one_event_and_nothing_install_scoped(world, tmp_path):
    app, db_path, event_id, _, _ = world
    out = tmp_path / "bike.sqlite3"
    conn = db.connect(db_path)
    admin.export_event(conn, event_id, out)
    conn.close()

    copy = sqlite3.connect(out)
    try:
        assert copy.execute("SELECT slug FROM event").fetchall() == [("bike",)]
        assert copy.execute("SELECT COUNT(*) FROM organization").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM roster").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM incident").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM event_note").fetchone()[0] == 1
        for table in ("user", "session", "access_token", "user_event",
                      "position", "raw_packet"):
            assert copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        event = copy.execute(
            "SELECT tracker_token, ingest_enabled FROM event").fetchone()
        assert event == (None, 0)
        # One self-contained file: a download must not need a -wal beside it.
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        copy.close()


def test_every_event_scoped_table_is_either_exported_or_left_out_on_purpose(tmp_path):
    """A table added later would otherwise be silently missing from every
    archive. Deciding means naming it in one list or the other."""
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    scoped = set()
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
        if "event_id" in columns:
            scoped.add(name)
    conn.close()
    exported = set(admin.EXPORTED_TABLES)
    assert not exported & set(admin.NOT_EXPORTED)
    assert scoped <= exported | set(admin.NOT_EXPORTED)


def test_the_export_opens_as_a_database_the_app_can_serve(world, tmp_path):
    """Viewing an archive is running the app against it - no importer."""
    _, db_path, event_id, _, _ = world
    out = tmp_path / "bike.sqlite3"
    conn = db.connect(db_path)
    admin.export_event(conn, event_id, out)
    conn.close()

    settings = Settings(callsign="", passcode="-1", host="h", port=1,
                        db_path=out, log_level="WARNING")
    with TestClient(web.create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
    copy = db.connect(out)
    assert copy.execute("SELECT name FROM event").fetchone()[0] == "Bike Fall"
    copy.close()


def test_the_export_downloads_from_setup(world):
    app, _, event_id, _, _ = world
    with TestClient(app) as client:
        _login(client)
        r = client.get(f"/api/setup/events/{event_id}/export")
    assert r.status_code == 200, r.text
    assert "bike" in r.headers["content-disposition"]
    assert r.content.startswith(b"SQLite format 3\x00")


def test_the_export_is_refused_without_access(world):
    app, _, event_id, _, _ = world
    with TestClient(app) as client:
        assert client.get(f"/api/setup/events/{event_id}/export").status_code in (401, 403)


# --- CLI ----------------------------------------------------------------------

def test_the_cli_exports_an_event_and_serve_reads_it_with_db(world, tmp_path,
                                                             monkeypatch, capsys):
    """The archive is only useful if someone can open it later, without the
    live server - so `serve --db` points the whole app at the file."""
    import sys
    import types

    from courseops import cli

    _, db_path, _, _, _ = world
    out = tmp_path / "kept.sqlite3"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APRS_CALLSIGN", "")
    assert cli.main(["export", "bike", str(out)]) == 0
    assert "kept.sqlite3" in capsys.readouterr().out

    seen = {}
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(
        run=lambda app, **k: seen.setdefault("db", app.state.settings.db_path)))
    cli.main(["serve", "--db", str(out), "--no-ingest"])
    assert seen["db"] == out
