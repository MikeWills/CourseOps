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

from courseops import db, web
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

    async def fake_feed(settings, slug, on_position=None, max_packets=None):
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

    async def explodes(settings, slug, on_position=None, max_packets=None):
        raise RuntimeError("no callsign configured")

    monkeypatch.setattr(web, "run_ingest", explodes)

    async def scenario():
        await app.state.start_ingest("alpha")
        for _ in range(10):                        # let it fail
            await asyncio.sleep(0)
        assert "no callsign" in app.state.ingest_errors.get("alpha", "")
        # and it is no longer claiming to run
        assert "alpha" not in app.state.ingest_tasks

    asyncio.run(scenario())
