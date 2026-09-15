"""The APRS-IS feed's lifecycle: one connection for the whole server.

Starting, stopping, displacing and forgetting the one ingest task, and the
tracking panel's view of it. Everything here reads the settings, the hub and
the task table from `app.state`, so the routes and the lifespan share one
implementation and the tests can drive it through `app.state.start_ingest`.

APRS-IS bans clients that open several connections, so there is exactly one
feed at a time: turning one event's tracking on turns any other's off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3

from fastapi import FastAPI

from . import db, progress, snapshot, tracker
from . import ingest as ingest_module
from .ingest import run_ingest

log = logging.getLogger(__name__)


def tracking_state(app: FastAPI, conn: sqlite3.Connection, event_id: int,
                   base_url: str = "") -> dict:
    """Everything needed to answer "is the feed on, and if not, why not?".

    The last part is the point. A switch that says "on" while nothing
    arrives is worse than no switch: the failure looks like a quiet net,
    and a quiet net on race day is something people act on.
    """
    row = conn.execute(
        "SELECT slug, ingest_enabled FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None:
        raise ValueError("No such event.")
    slug = row["slug"]
    tracked = db.tracked_station_keys(conn, event_id)
    area = progress.CourseIndex.for_event(conn, event_id).area(
        ingest_module.AREA_MARGIN_M)
    # callsign_problem says WHY it cannot be used, not merely that it
    # cannot - "still the placeholder N0CALL" is a different fix from
    # "not set", and the person reading this is not at a terminal.
    settings = app.state.settings
    problem = settings.callsign_problem
    return {
        "area_mi": round(area[2] / 1609.344, 1) if area else None,
        "enabled": bool(row["ingest_enabled"]),
        "running": slug in app.state.ingest_tasks,
        "callsign": settings.callsign or "",
        "has_callsign": problem is None,
        "callsign_problem": problem or "",
        "tracked": len(tracked),
        "filter": access_filter_preview(tracked, area),
        "error": app.state.ingest_errors.get(slug, ""),
        "phone": phone_tracking_state(conn, event_id, slug, base_url),
    }


def phone_tracking_state(conn: sqlite3.Connection, event_id: int, slug: str,
                         base_url: str) -> dict:
    """The phone tracking half of the Tracking tab: the one URL, the QR
    code that configures OwnTracks with it, and the designators to print
    beside it - because the whole scheme depends on the string a person
    types matching the roster, and a person guessing at the wording is the
    failure mode. Off (no token) means the endpoint answers 404."""
    token = db.tracker_token(conn, event_id)
    designators = [
        {"station_key": row["station_key"],
         "display_label": row["display_label"]}
        for row in conn.execute(
            "SELECT station_key, display_label FROM roster"
            " WHERE event_id = ? AND tracked_by = ?"
            " ORDER BY station_key",
            (event_id, db.TRACKED_BY_PHONE),
        ).fetchall()
    ]
    state = {"enabled": bool(token), "designators": designators}
    if token:
        url = f"{base_url.rstrip('/')}/track/{slug}/{token}"
        link = tracker.owntracks_link(url)
        state.update({
            "url": url,
            "owntracks_link": link,
            "qr_svg": tracker.qr_svg(link),
        })
    return state


def access_filter_preview(tracked, area=None) -> str:
    """The APRS-IS filter this roster produces, for the operator to see.

    Shown because the commonest silent failure here is an empty or wrong
    filter: the feed connects, nothing matches, and the map stays blank
    while everything reports healthy.
    """
    try:
        from .aprsis import build_filter
        return build_filter(
            sorted(tracked),
            area=(area[0], area[1], area[2] / 1000.0) if area else None)
    except Exception:                              # noqa: BLE001
        return ""


async def ingest_for(app: FastAPI, slug: str) -> None:
    settings = app.state.settings
    conn = db.connect(settings.db_path)
    event = db.get_event(conn, slug)
    if event is None:
        conn.close()
        # Raised rather than logged and returned, so the supervisor
        # records it and the tracking panel can say so.
        raise ingest_module.IngestError(
            f"No event with slug {slug!r}. Create it first.")
    known_keys = set(db.all_station_keys(conn, event["id"]))
    known_keys |= db.bound_station_keys(conn, event["id"])
    # Course geometry is loaded once for the life of the ingest task rather
    # than per packet. Courses are set up before the event and do not change
    # while it runs; restart the server if one is re-imported mid-event.
    index = progress.CourseIndex.for_event(conn, event["id"])
    conn.close()

    on_position = snapshot.make_position_handler(
        app.state.hub, known_keys, index)
    on_nearby = snapshot.make_nearby_handler(
        app.state.hub, app.state.nearby, index)
    await run_ingest(settings, slug, on_position=on_position,
                     on_nearby=on_nearby)


async def supervise_ingest(app: FastAPI, slug: str) -> None:
    """Run one feed, and remember why it stopped."""
    try:
        await ingest_for(app, slug)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:                   # noqa: BLE001
        # A missing callsign lands here, and so does anything APRS-IS does
        # that the client cannot recover from. Losing it to the log alone
        # means the switch says "on" and nothing arrives.
        #
        # BaseException, not Exception, and the difference is the whole
        # server: asyncio re-raises SystemExit and KeyboardInterrupt out
        # of a task and out of the loop, so a feed that once signalled
        # "nothing to listen for" with SystemExit took every role page
        # down with it, and the persisted switch restarted it into the
        # same crash. Nothing that happens inside one feed is allowed to
        # decide that the site stops.
        app.state.ingest_errors[slug] = str(exc) or exc.__class__.__name__
        log.error("Ingest for %r stopped: %s", slug, exc)
    finally:
        app.state.ingest_tasks.pop(slug, None)


async def start_ingest(app: FastAPI, slug: str) -> bool:
    """Start one feed, replacing any other. True if it is running.

    APRS-IS bans clients that open many connections, so there is exactly
    one for the whole server - which means turning a feed on turns any
    other one off, rather than quietly running two.

    The feed does its refusals - no callsign, no event, nothing to listen
    for - before its first real await, so one turn of the loop is enough
    to know whether it got as far as connecting. The caller persists the
    switch only on True: a flag written for a feed that never started is
    what turned one bad press into a restart loop.
    """
    if slug in app.state.ingest_tasks:
        return True
    for running in list(app.state.ingest_tasks):
        if running != slug:
            await displace_ingest(app, running, by=slug)
    app.state.ingest_errors.pop(slug, None)
    app.state.ingest_tasks[slug] = asyncio.create_task(
        supervise_ingest(app, slug), name=f"ingest:{slug}")
    await asyncio.sleep(0)
    return slug in app.state.ingest_tasks


async def stop_ingest(app: FastAPI, slug: str) -> None:
    task = app.state.ingest_tasks.pop(slug, None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _event_name(app: FastAPI, slug: str) -> str:
    conn = db.connect(app.state.settings.db_path)
    try:
        event = db.get_event(conn, slug)
        return event["name"] if event else slug
    finally:
        conn.close()


async def displace_ingest(app: FastAPI, slug: str, by: str) -> None:
    """Turn one event's feed off because another's is going on.

    The persisted switch is what the next boot acts on, so the displaced
    event's flag has to come off with the feed: left on, the boot found
    two flagged, started the first and cancelled it for the second, and
    the displaced tab read "on - but not connected" with nothing to say
    why. The reason goes where the tab already looks for one.
    """
    await stop_ingest(app, slug)
    conn = db.connect(app.state.settings.db_path)
    try:
        db.set_ingest_enabled(conn, slug, False)
    finally:
        conn.close()
    app.state.ingest_errors[slug] = (
        f"Tracking was turned on for {_event_name(app, by)}, and there is "
        "one APRS-IS connection for the whole server.")


async def forget_ingest(app: FastAPI, slug: str, event_id: int) -> None:
    """The event is gone; nothing about its feed may outlive it.

    Otherwise the connection keeps a wildcard filter on the deleted
    event's volunteers until the next restart, and re-creating the slug
    finds a feed "already running" that is bound to the dead event id.
    The nearby list is keyed by event id, the rest by slug.
    """
    await stop_ingest(app, slug)
    app.state.ingest_errors.pop(slug, None)
    app.state.nearby.pop(event_id, None)
