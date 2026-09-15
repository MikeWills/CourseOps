"""The field API: what the live map talks to, by role link.

Every route is /api/{event_slug}/{token}/... and the WebSocket is
/ws/{event_slug}/{token}. The token in the path IS the credential, resolved
by `FieldAccess`; a write names the capability it needs with
`Depends(needs(...))`, and a token lacking it gets 403 with the reason. The
hub and the nearby list are reached through `request.app.state`.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from urllib.parse import parse_qsl

from fastapi import (APIRouter, Depends, FastAPI, HTTPException, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse

from . import access, db, incidents, leaders, progress, snapshot, tracker
from .deps import Conn, FieldAccess, Grant, json_body, needs, raw_body

log = logging.getLogger(__name__)

router = APIRouter()

# How long a WebSocket may carry nothing before the server says something on
# its own. A phone cannot tell a quiet net from a dead socket - both are
# silence, and the badge reads "Live" for both - and a socket that dies
# without a close frame (a phone that slept, a NAT that forgot) never fires
# `close`. Uvicorn pings at the protocol level, which the browser answers
# without telling the page; this is the heartbeat the page can see. The
# client gives up on a socket after three of these have failed to arrive.
HEARTBEAT_SECONDS = 60


@router.get("/api/{event_slug}/{token}/manifest.webmanifest")
async def manifest(
    event_slug: str, token: str, auth: FieldAccess
) -> JSONResponse:
    """Per-event, per-role manifest.

    `start_url` points back at this exact role link, so "Add to Home Screen"
    lands on the right event with the right permissions. That does mean the
    bearer token is saved onto the phone's home screen, which is consistent
    with the link model but worth knowing - see docs/RUNBOOK.md.
    """
    conn, granted = auth
    event = conn.execute(
        "SELECT name FROM event WHERE id = ?", (granted.event_id,)
    ).fetchone()

    start = f"/e/{event_slug}/{token}"
    return JSONResponse(
        {
            "name": f"Course Ops - {event['name']}",
            # Home screen labels truncate around 12 characters; the role is
            # the useful half when someone holds two links.
            "short_name": granted.role_label,
            "description": "Ham radio event tracking and communications",
            "start_url": start,
            "scope": start,
            "display": "standalone",
            "orientation": "any",
            "background_color": "#0B2545",
            "theme_color": "#0B2545",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-maskable-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "maskable"},
                {"src": "/static/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        },
        media_type="application/manifest+json",
    )

# --- phone tracking ----------------------------------------------------
#
# Not a role link. /track/{slug}/{token} is the one URL a tracking app on a
# non-ham's phone posts to; the token is the event's `tracker_token`, shared
# by everyone the event tracks by phone, and each person identifies
# themselves by the designator they typed into the app. The roster is the
# allowlist: a known designator is stored and fanned out like an APRS
# packet, an unknown one goes to the nearby list for NCS to match, and the
# app is told 200 either way - it has nothing to act on, and an app that
# gets errors may stop sending. See docs/phone-tracking.md.

def _tracked_event(conn, event_slug: str, token: str):
    """The event this tracker URL belongs to, or 404 - never 403."""
    row = conn.execute(
        "SELECT id, tracker_token FROM event WHERE slug = ?", (event_slug,)
    ).fetchone()
    if (row is None or not row["tracker_token"] or not token
            or not secrets.compare_digest(row["tracker_token"], token)):
        raise HTTPException(status_code=404)
    return row["id"]


def _file_tracker_report(conn, event_id: int, designator: str, report):
    """The SQLite half of receiving a report, off the loop.

    Returns (index, what, report): `what` is "nearby" for a designator the
    roster does not know, "position" for a stored fix worth publishing, or
    None for a duplicate or an out-of-order fix from a buffered backlog -
    stored, because the track is real, but not drawn, because the marker
    would walk backwards.
    """
    entry = tracker.match_roster(conn, event_id, designator)
    index = progress.CourseIndex.for_event(conn, event_id)
    if entry is None:
        return index, "nearby", report
    report = tracker.attribute(report, entry)
    newest = tracker.is_newest(conn, event_id, report)
    stored = tracker.store(conn, event_id, report)
    if stored is None or not newest:
        return index, None, report
    return index, "position", report


async def _receive_tracker_report(request: Request, conn, event_id: int,
                                  designator: str, report) -> None:
    """Store or hold one report and tell the browsers, like the feed does."""
    app = request.app
    index, what, report = await asyncio.to_thread(
        _file_tracker_report, conn, event_id, designator, report)
    if what == "nearby":
        # Held in memory for NCS, never written. Same handler as an
        # unknown APRS station, so the Match button works unchanged.
        on_nearby = snapshot.make_nearby_handler(
            app.state.hub, app.state.nearby, index)
        await on_nearby(event_id, report)
    elif what == "position":
        await app.state.hub.publish(
            event_id, snapshot.position_message_for(report, index))


@router.get("/track/{event_slug}/{token}")
async def track_get(event_slug: str, token: str, request: Request,
                    conn: Conn) -> JSONResponse:
    """Traccar Client / OsmAnd protocol: the fix is in the query string."""
    event_id = _tracked_event(conn, event_slug, token)
    try:
        designator, report = tracker.parse_osmand(
            request.query_params, raw=str(request.url.query))
    except tracker.Rejected as why:
        log.debug("Tracker report rejected: %s", why)
        return JSONResponse([])
    await _receive_tracker_report(request, conn, event_id, designator, report)
    return JSONResponse([])


@router.post("/track/{event_slug}/{token}")
async def track_post(event_slug: str, token: str, request: Request,
                     conn: Conn) -> JSONResponse:
    """OwnTracks (JSON) or Traccar Client (form-encoded) posting the fix.

    Answers an empty JSON array, which is what OwnTracks expects back
    (a list of messages for the app; we have none) and Traccar ignores.
    """
    event_id = _tracked_event(conn, event_slug, token)
    body = await raw_body(request)
    content_type = request.headers.get("content-type", "").lower()
    try:
        if "json" in content_type or body.lstrip().startswith(b"{"):
            parsed = tracker.parse_owntracks(body)
            if parsed is None:
                return JSONResponse([])
            designator, report = parsed
        else:
            params = dict(parse_qsl(body.decode("utf-8", "replace")))
            params = {**dict(request.query_params), **params}
            designator, report = tracker.parse_osmand(params, raw=body.decode("utf-8", "replace"))
    except tracker.Rejected as why:
        log.debug("Tracker report rejected: %s", why)
        return JSONResponse([])
    await _receive_tracker_report(request, conn, event_id, designator, report)
    return JSONResponse([])


# --- api ---------------------------------------------------------------


@router.get("/api/{event_slug}/{token}/state")
async def state(
    event_slug: str, token: str, auth: FieldAccess, request: Request
) -> JSONResponse:
    conn, granted = auth
    # Off the loop. The snapshot is the one heavy read in the live
    # app - 90 ms on the demo event, seconds on the real course - and
    # while it was being built on the loop nothing else moved: no
    # WebSocket send, no ingest, no other phone. A setup save resyncs
    # every phone at once, so twelve phones were twelve builds in a
    # row with positions frozen for the sum of them.
    payload = await asyncio.to_thread(
        snapshot.build_state, conn, granted.event_id)
    # The public, heard near the course. Only for a role that can
    # match or dismiss them; nobody else needs a list of who is
    # driving past. Same connection: opening one is not free, and
    # this route used to open three.
    if granted.can(access.CAP_SSID):
        # Callsigns on an SSID the roster does not name. Surfaced
        # in the UI rather than left to a command someone has to
        # remember: the failure it catches is silent, and a check
        # that must be remembered will be forgotten. Only NCS
        # renders it, and the field links do not need a list of
        # which roster callsigns own which digipeaters.
        payload["ssid_alerts"] = snapshot.ssid_alerts(conn, granted.event_id)
        payload["nearby"] = snapshot.nearby_for(
            conn, granted.event_id, request.app.state.nearby)
        # What has been ignored, so a mis-tap on Ignore can be
        # undone. An ignored station is silent in every other list,
        # which is the point of ignoring it and also what makes the
        # mistake invisible.
        payload["ignored"] = [
            dict(row) for row in db.exclusions(conn, granted.event_id)
        ]
    payload["role"] = granted.role
    payload["role_label"] = granted.role_label
    payload["can_write"] = granted.can_write
    # Per capability, so the client shows exactly the controls this role can
    # actually use. A button the server would refuse is worse than no button.
    payload["capabilities"] = sorted(granted.capabilities)
    # Pickups and course notes go to the roles that work or report them.
    # Staff hold neither: race staff and the organizer read the map for
    # where everyone is, and a queue of runners who could not continue is
    # the club's business during the race, not theirs. The report page
    # is where the organizer gets the counts afterwards.
    if not granted.can(access.CAP_INCIDENT_REPORT):
        payload.pop("incidents", None)
    return JSONResponse(payload)

# --- writes ------------------------------------------------------------
#
# Every mutation names the capability it needs (`Depends(needs(...))`)
# and goes through one check, so widening a role is a change to
# access.ROLE_CAPABILITIES rather than a rewrite of each endpoint. It
# used to be a single yes/no; SAG needs to work its pickup queue without
# being able to revoke a link or edit the roster.


@router.post("/api/{event_slug}/{token}/station/{station_key}/status")
async def set_station_status(
    event_slug: str, token: str, station_key: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_STATIONS))
) -> JSONResponse:
    conn, granted = auth
    body = await json_body(request)

    try:
        op_status = (db.clean_text(body.get("op_status")) or "").lower()
        # Free-text initials typed once per shift. A log annotation for
        # handover, never authentication - do not start trusting it as
        # identity. Same cap as the incident and sighting logs, so one
        # shift's entries match on a handover read: this was 12 while
        # the others were 24, and "Christopher Wainwright" signed a
        # pickup whole and a status change as "Christopher ".
        changed_by = db.clean_text(body.get("changed_by"),
                                   incidents.MAX_WHO_LENGTH)
        row = db.set_op_status(
            conn, granted.event_id, station_key, op_status, changed_by
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    payload = {
        "type": "station_status",
        "station_key": row["station_key"],
        # The client keys its roster by what it HEARS (the bound SSID for
        # a bare-callsign entry), so a message keyed only by the roster's
        # own key misses that map on every screen but the one that
        # pressed the button. Same helper as the snapshot uses.
        "tracking_key": db.tracking_key(row),
        "op_status": row["op_status"],
        "op_status_at": row["op_status_at"],
        "op_status_by": row["op_status_by"],
        "op_status_label": db.op_status_label(row["category"], row["op_status"]),
    }
    # Everyone watching sees it immediately, including the read-only roles.
    await request.app.state.hub.publish(granted.event_id, payload)
    return JSONResponse(payload)


async def _publish_state_hint(app: FastAPI, event_id: int) -> None:
    """Ask every client to reload full state.

    Adopting or ignoring an SSID rewrites the roster, which is more than an
    incremental message can express. A resync is cheap and cannot leave a
    client half-updated.
    """
    await app.state.hub.publish(event_id, {"type": "resync"})


async def _publish_incident(app: FastAPI, event_id: int, row, kind: str) -> dict:
    """Broadcast one incident and return what was sent, minus the framing.

    The route answers with that same dict. The 201 from a create used to be
    the bare row while the socket message carried `course_position`, so a
    browser that put its own response up had a pickup with no mile until
    its own broadcast came back round and overwrote it - two shapes for one
    thing, and the client had grown a workaround for their disagreeing.
    """
    conn = db.connect(app.state.settings.db_path)
    try:
        index = progress.CourseIndex.for_event(conn, event_id)
    finally:
        conn.close()
    payload = incidents.Incident(row).as_dict()
    payload["course_position"] = snapshot.course_position(
        index, row["lat"], row["lon"])
    # Same audience as the snapshot: a role that never receives the list
    # must not be handed its entries one at a time either.
    await app.state.hub.publish(
        event_id, {**payload, "type": "incident", "change": kind},
        requires=access.CAP_INCIDENT_REPORT)
    return payload


# Reporting one is not the same permission as working the queue. Every role
# is somewhere an incident can happen, so any of them may open one and
# describe it; only NCS and SAG may move it along or take it off the board.
@router.post("/api/{event_slug}/{token}/incidents")
async def create_incident(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_INCIDENT_REPORT))
) -> JSONResponse:
    conn, granted = auth
    body = await json_body(request)
    try:
        row = incidents.create(
            conn, granted.event_id,
            lat=float(body.get("lat")), lon=float(body.get("lon")),
            bib=body.get("bib"), note=body.get("note"),
            poi_id=body.get("poi_id"), by=body.get("changed_by"),
            kind=(body.get("kind") or incidents.KIND_PICKUP),
        )
    except (incidents.IncidentError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = await _publish_incident(request.app, granted.event_id, row, "created")
    return JSONResponse(payload, status_code=201)


@router.post("/api/{event_slug}/{token}/incidents/{incident_id}/status")
async def set_incident_status(
    event_slug: str, token: str, incident_id: int, request: Request,
    auth: Grant = Depends(needs(access.CAP_INCIDENTS))
) -> JSONResponse:
    conn, granted = auth
    body = await json_body(request)
    try:
        row = incidents.set_status(
            conn, granted.event_id, incident_id,
            str(body.get("status", "")).strip().lower(),
            by=body.get("changed_by"),
        )
    except (incidents.IncidentError, ValueError) as exc:
        # ValueError: `changed_by` sent as a list or an object, which the
        # text cleaner refuses rather than storing "[5]" in the log.
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        await _publish_incident(request.app, granted.event_id, row, "status"))


@router.post("/api/{event_slug}/{token}/incidents/{incident_id}/delete")
async def delete_incident(
    event_slug: str, token: str, incident_id: int, request: Request,
    auth: Grant = Depends(needs(access.CAP_INCIDENTS))
) -> JSONResponse:
    """Remove a pickup or a course note that should never have existed."""
    conn, granted = auth
    try:
        row = incidents.delete(conn, granted.event_id, incident_id)
    except incidents.IncidentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Every other browser has this in its list and on its map, and
    # nothing else will ever mention it again.
    await _publish_incident(request.app, granted.event_id, row, "deleted")
    return JSONResponse({"deleted": incident_id})


@router.post("/api/{event_slug}/{token}/incidents/{incident_id}")
async def update_incident(
    event_slug: str, token: str, incident_id: int, request: Request,
    auth: Grant = Depends(needs(access.CAP_INCIDENT_REPORT))
) -> JSONResponse:
    conn, granted = auth
    body = await json_body(request)
    fields = {k: v for k, v in body.items()
              if k in {"bib", "note", "assigned_to", "lat", "lon"}}
    try:
        row = incidents.update(
            conn, granted.event_id, incident_id,
            by=body.get("changed_by"), **fields,
        )
    except (incidents.IncidentError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        await _publish_incident(request.app, granted.event_id, row, "edited"))


@router.post("/api/{event_slug}/{token}/ssid/adopt")
async def adopt_ssid(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_SSID))
) -> JSONResponse:
    """Point a roster entry at the SSID its operator is actually using."""
    conn, granted = auth
    body = await json_body(request)
    try:
        row = db.change_station_key(
            conn, granted.event_id,
            str(body.get("from_station_key", "")),
            str(body.get("to_station_key", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = {"station_key": row["station_key"],
               "display_label": row["display_label"]}
    request.app.state.nearby.get(granted.event_id, {}).pop(
        str(body.get("to_station_key", "")).strip().upper(), None)
    await _publish_state_hint(request.app, granted.event_id)
    return JSONResponse(payload)


@router.post("/api/{event_slug}/{token}/ssid/unbind")
async def unbind_ssid(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_SSID))
) -> JSONResponse:
    """Undo a match: the roster entry goes back to waiting for a station."""
    conn, granted = auth
    body = await json_body(request)
    station_key = str(body.get("station_key", "")).strip().upper()
    row = conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (granted.event_id, station_key),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{station_key} is not on the roster.")
    db.unbind_station(conn, granted.event_id, station_key)
    await _publish_state_hint(request.app, granted.event_id)
    return JSONResponse({"station_key": row["station_key"],
                         "display_label": row["display_label"],
                         "was": row["bound_key"]})


@router.post("/api/{event_slug}/{token}/ssid/ignore")
async def ignore_ssid(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_SSID))
) -> JSONResponse:
    """Dismiss an SSID: a digipeater, igate or home station."""
    conn, granted = auth
    body = await json_body(request)
    station_key = str(body.get("station_key", "")).strip()
    if not station_key:
        raise HTTPException(status_code=400, detail="A station_key is required.")
    try:
        db.exclude_station(conn, granted.event_id, station_key,
                           body.get("reason") or "dismissed from the map")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.nearby.get(granted.event_id, {}).pop(station_key.upper(), None)
    await _publish_state_hint(request.app, granted.event_id)
    return JSONResponse({"ignored": station_key.upper()})


@router.post("/api/{event_slug}/{token}/ssid/unignore")
async def unignore_ssid(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_SSID))
) -> JSONResponse:
    """Undo an Ignore. The station comes back the next time it is heard."""
    conn, granted = auth
    body = await json_body(request)
    station_key = str(body.get("station_key", "")).strip()
    if not station_key:
        raise HTTPException(status_code=400, detail="A station_key is required.")
    removed = db.unexclude_station(conn, granted.event_id, station_key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{station_key.upper()} was not ignored.")
    await _publish_state_hint(request.app, granted.event_id)
    return JSONResponse({"unignored": station_key.upper()})


@router.get("/api/{event_slug}/{token}/station-log")
async def station_log(
    event_slug: str, token: str, auth: FieldAccess,
    station_key: str | None = None
) -> JSONResponse:
    """Operational status history, for shift handover and after-action.

    Readable by every role: the incoming operator needs it regardless of
    whether they can write.

    API-only: no screen in the live app fetches this yet (the panel shows
    the current status and its age). It is the read side of
    `roster_status_log`, which is append-only precisely so that a history
    view can be added later without rebuilding anything; the tests reach
    it here. Deleting it would leave that log write-only.
    """
    conn, granted = auth
    entries = [
        dict(row)
        for row in db.op_status_log(conn, granted.event_id, station_key)
    ]
    return JSONResponse({"entries": entries})


@router.get("/api/{event_slug}/{token}/incidents/{incident_id}/log")
async def incident_log(
    event_slug: str, token: str, incident_id: int,
    auth: Grant = Depends(needs(access.CAP_INCIDENT_REPORT))
) -> JSONResponse:
    # Readable by every role that sees the queue: the log is what a
    # shift handover reads. API-only for now, like station-log above:
    # the queue shows the current status and its age, and the history
    # behind it is reachable here and from the tests.
    conn, granted = auth
    try:
        incidents.get(conn, granted.event_id, incident_id)
        entries = [
            dict(row)
            for row in incidents.log_for(conn, incident_id)
        ]
    except incidents.IncidentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse({"entries": entries})


async def _publish_leaders(app: FastAPI, event_id: int) -> None:
    conn = db.connect(app.state.settings.db_path)
    try:
        index = progress.CourseIndex.for_event(conn, event_id)
        payload = {
            "type": "leaders",
            "leaders": [e.as_dict() for e in leaders.for_event(conn, event_id, index)],
        }
    finally:
        conn.close()
    await app.state.hub.publish(event_id, payload)


@router.post("/api/{event_slug}/{token}/leaders/sighting")
async def record_leader(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_LEADERS))
) -> JSONResponse:
    """Log that a division's leader passed an aid station.

    This only ever comes from an operator reporting on the net - there is no
    tracker on the front runner - so it is a report, not a measurement.
    """
    conn, granted = auth
    body = await json_body(request)
    try:
        leaders.record_sighting(
            conn, granted.event_id,
            course_id=int(body.get("course_id")),
            division=body.get("division"),
            poi_id=int(body.get("poi_id")),
            bib=body.get("bib"),
            by=body.get("changed_by"),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _publish_leaders(request.app, granted.event_id)
    return JSONResponse({"ok": True}, status_code=201)


@router.post("/api/{event_slug}/{token}/leaders/undo")
async def undo_leader(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_LEADERS))
) -> JSONResponse:
    """Remove the most recent sighting. Mis-taps happen on race day."""
    conn, granted = auth
    body = await json_body(request)
    try:
        removed = leaders.undo_last_sighting(
            conn, granted.event_id,
            int(body.get("course_id")), str(body.get("division", "")),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if removed:
        await _publish_leaders(request.app, granted.event_id)
    return JSONResponse({"removed": removed})


@router.post("/api/{event_slug}/{token}/leaders/reset")
async def reset_leader(
    event_slug: str, token: str, request: Request,
    auth: Grant = Depends(needs(access.CAP_LEADERS))
) -> JSONResponse:
    """Clear every sighting for one race and division.

    Undo walks back one report at a time, which is no use to a club that
    rehearsed the panel the week before and wants a clean start.
    """
    conn, granted = auth
    body = await json_body(request)
    try:
        removed = leaders.clear_sightings(
            conn, granted.event_id,
            int(body.get("course_id")), str(body.get("division", "")),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if removed:
        await _publish_leaders(request.app, granted.event_id)
    return JSONResponse({"removed": removed})

# --- live feed ---------------------------------------------------------


@router.websocket("/ws/{event_slug}/{token}")
async def live(websocket: WebSocket, event_slug: str, token: str) -> None:
    app = websocket.app
    conn = db.connect(app.state.settings.db_path)
    granted = access.resolve(conn, event_slug, token)
    conn.close()
    if granted is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    subscription = app.state.hub.subscribe(
        granted.event_id, granted.capabilities)
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    subscription.queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                message = {"type": "heartbeat"}
            await websocket.send_json(message)
            if message.get("type") == "resync":
                # Whatever the hub dropped for this phone before now is
                # covered by the snapshot it is about to fetch.
                subscription.caught_up()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - transport level
        log.debug("WebSocket closed: %s", exc)
    finally:
        app.state.hub.unsubscribe(subscription)

