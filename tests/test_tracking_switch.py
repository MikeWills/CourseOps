"""Turning the APRS-IS feed on and off from the setup screen.

Two things here must not be wrong, and neither is visible when it is:

  * exactly one connection, ever - APRS-IS bans clients that open several
  * the switch survives a restart, because a deploy restarts the service and
    a feed that failed to come back looks exactly like a quiet net

These drive the real functions on the app, with the feed itself replaced, so
nothing here opens a socket to APRS-IS.
"""

from __future__ import annotations

import asyncio

import pytest

from courseops import db, ingest, web
from courseops.config import Settings


@pytest.fixture
def app_with_events(tmp_path, monkeypatch):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.create_event(conn, "alpha", "Alpha")
    db.create_event(conn, "bravo", "Bravo")
    conn.close()

    running: set[str] = set()
    started: list[str] = []

    async def fake_feed(settings, slug, on_position=None, max_packets=None,
                        on_nearby=None):
        started.append(slug)
        running.add(slug)
        try:
            await asyncio.Event().wait()           # until cancelled
        finally:
            running.discard(slug)

    monkeypatch.setattr(web, "run_ingest", fake_feed)

    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    return web.create_app(settings), running, started, db_path


def test_a_new_event_does_not_track(tmp_path):
    """Off outside race day is the intended state, not an oversight: the
    filter matches an operator's callsign wherever they are."""
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    db.create_event(conn, "e", "Event")
    assert db.events_wanting_ingest(conn) == []


def test_the_switch_persists(tmp_path):
    """Why this is a column and not a variable: a deploy restarts the service
    mid-event and the feed has to come back on its own."""
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    db.init_schema(conn)
    db.create_event(conn, "e", "Event")
    db.set_ingest_enabled(conn, "e", True)
    conn.close()

    conn = db.connect(path)                        # as if restarted
    assert db.events_wanting_ingest(conn) == ["e"]
    db.set_ingest_enabled(conn, "e", False)
    assert db.events_wanting_ingest(conn) == []


def test_an_unknown_slug_changes_nothing(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    db.create_event(conn, "e", "Event")
    db.set_ingest_enabled(conn, "nope", True)
    assert db.events_wanting_ingest(conn) == []


def test_starting_one_feed_stops_any_other(app_with_events):
    """APRS-IS bans clients that open many connections, so there is exactly
    one for the whole server."""
    app, running, started, _ = app_with_events

    async def scenario():
        await app.state.start_ingest("alpha")
        await asyncio.sleep(0)
        assert running == {"alpha"}

        await app.state.start_ingest("bravo")
        await asyncio.sleep(0)
        assert running == {"bravo"}, "two feeds were running at once"
        assert set(app.state.ingest_tasks) == {"bravo"}

        await app.state.stop_ingest("bravo")
        assert running == set()

    asyncio.run(scenario())


def test_starting_the_same_feed_twice_opens_one_connection(app_with_events):
    """A double press on the switch must not double the connections."""
    app, running, started, _ = app_with_events

    async def scenario():
        await app.state.start_ingest("alpha")
        await app.state.start_ingest("alpha")
        await asyncio.sleep(0)
        assert started == ["alpha"]
        await app.state.stop_ingest("alpha")

    asyncio.run(scenario())


def test_stopping_something_that_is_not_running_is_harmless(app_with_events):
    app, _, _, _ = app_with_events
    asyncio.run(app.state.stop_ingest("alpha"))


def test_a_feed_that_dies_records_why(app_with_events, monkeypatch):
    """A switch that says "on" while nothing arrives is worse than no switch:
    the failure looks like a quiet net, and people act on a quiet net."""
    app, _, _, _ = app_with_events

    async def explodes(settings, slug, on_position=None, max_packets=None,
                       on_nearby=None):
        raise RuntimeError("no callsign configured")

    monkeypatch.setattr(web, "run_ingest", explodes)

    async def scenario():
        started = await app.state.start_ingest("alpha")
        for _ in range(10):                        # let it fail
            await asyncio.sleep(0)
        assert started is False
        assert "no callsign" in app.state.ingest_errors.get("alpha", "")
        # and it is no longer claiming to run
        assert "alpha" not in app.state.ingest_tasks

    asyncio.run(scenario())


def test_a_feed_that_exits_the_interpreter_is_recorded_not_obeyed(
        app_with_events, monkeypatch):
    """`SystemExit` is a BaseException, and asyncio re-raises those out of
    the event loop: one feed that could not start took the whole server
    down, every role page with it, and the persisted switch restarted it
    into the same crash. The supervisor has to swallow everything but its
    own cancellation."""
    app, _, _, _ = app_with_events

    async def exits(settings, slug, on_position=None, max_packets=None,
                    on_nearby=None):
        raise SystemExit("Event 'alpha' has no APRS-expecting roster entries")

    monkeypatch.setattr(web, "run_ingest", exits)

    async def scenario():
        started = await app.state.start_ingest("alpha")
        for _ in range(10):
            await asyncio.sleep(0)
        assert started is False
        assert "no APRS-expecting" in app.state.ingest_errors.get("alpha", "")
        assert "alpha" not in app.state.ingest_tasks
        return "the loop survived"

    assert asyncio.run(scenario()) == "the loop survived"


def test_the_real_feed_refuses_an_empty_event_without_exiting(tmp_path):
    """The real `run_ingest` on an event with no roster, no course and no
    extra filter: it must say so with an ordinary exception, not with
    SystemExit, which the CLI alone is allowed to turn into an exit code."""
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.create_event(conn, "empty", "Empty")
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")

    with pytest.raises(ingest.IngestError) as raised:
        asyncio.run(ingest.run_ingest(settings, "empty"))
    assert not isinstance(raised.value, SystemExit)
    assert "expected to beacon" in str(raised.value)

    with pytest.raises(ingest.IngestError):
        asyncio.run(ingest.run_ingest(settings, "no-such-event"))


def test_no_callsign_is_an_ordinary_error_too(tmp_path):
    settings = Settings(callsign="", passcode="-1", host="h", port=1,
                        db_path=tmp_path / "t.sqlite3", log_level="WARNING")
    with pytest.raises(ingest.IngestError) as raised:
        asyncio.run(ingest.run_ingest(settings, "whatever"))
    assert not isinstance(raised.value, SystemExit)
    assert "APRS_CALLSIGN" in str(raised.value)


def test_a_flagged_event_that_cannot_start_does_not_stop_the_server(tmp_path):
    """The boot-loop half of the same bug: an event left flagged on while
    its feed cannot start (a callsign lost between deploys, a course not yet
    imported) must come up as "on - but not connected" with the reason, not
    as a service that exits under systemd until someone edits the database.
    This runs the REAL run_ingest against an empty event."""
    from fastapi.testclient import TestClient

    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.create_event(conn, "empty", "Empty")
    db.set_ingest_enabled(conn, "empty", True)
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    app = web.create_app(settings)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert "expected to beacon" in app.state.ingest_errors["empty"]
        assert "empty" not in app.state.ingest_tasks


def _admin_client(app, db_path):
    from fastapi.testclient import TestClient
    from courseops import users
    conn = db.connect(db_path)
    users.create_organization(conn, "club", "Club")
    users.create_user(conn, "mike", "a-long-enough-password",
                      users.ROLE_SYSTEM_ADMIN)
    conn.close()
    client = TestClient(app)
    client.__enter__()
    client.post("/api/setup/login",
                json={"username": "mike", "password": "a-long-enough-password"})
    return client


def test_the_switch_refuses_an_event_with_nothing_to_listen_for(app_with_events):
    """Refuse rather than start a task that dies: a switch reading "on" with
    nothing behind it is worse than no switch, and the flag must not be
    persisted for a feed that never started - that is what turned one bad
    press into a restart loop."""
    app, running, started, db_path = app_with_events
    client = _admin_client(app, db_path)
    try:
        conn = db.connect(db_path)
        event_id = db.get_event(conn, "alpha")["id"]
        conn.close()

        response = client.post(f"/api/setup/events/{event_id}/tracking",
                               json={"enabled": True})
        assert response.status_code == 400
        assert "expected to beacon" in response.json()["detail"]
        assert started == []

        conn = db.connect(db_path)
        assert db.events_wanting_ingest(conn) == []
        state = client.get(f"/api/setup/events/{event_id}/tracking").json()
        assert state["enabled"] is False and state["running"] is False

        # Give it something to listen for and the same press works.
        db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Sweep", "sweep")
        conn.close()
        response = client.post(f"/api/setup/events/{event_id}/tracking",
                               json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert started == ["alpha"]
        conn = db.connect(db_path)
        assert db.events_wanting_ingest(conn) == ["alpha"]
        conn.close()
    finally:
        client.__exit__(None, None, None)


def test_the_flag_is_persisted_only_after_the_feed_started(
        app_with_events, monkeypatch):
    """A feed that dies on its first step leaves the switch OFF and the
    reason on the panel, rather than a persisted "on" that the next boot
    retries into the same failure."""
    app, running, started, db_path = app_with_events

    async def dies(settings, slug, on_position=None, max_packets=None,
                   on_nearby=None):
        raise ingest.IngestError("APRS-IS refused the login")

    monkeypatch.setattr(web, "run_ingest", dies)
    client = _admin_client(app, db_path)
    try:
        conn = db.connect(db_path)
        event_id = db.get_event(conn, "alpha")["id"]
        db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Sweep", "sweep")
        conn.close()

        response = client.post(f"/api/setup/events/{event_id}/tracking",
                               json={"enabled": True})
        assert response.status_code == 400
        assert "refused the login" in response.json()["detail"]

        conn = db.connect(db_path)
        assert db.events_wanting_ingest(conn) == []
        conn.close()
        state = client.get(f"/api/setup/events/{event_id}/tracking").json()
        assert state["enabled"] is False
        assert "refused the login" in state["error"]
    finally:
        client.__exit__(None, None, None)
