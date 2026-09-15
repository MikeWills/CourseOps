"""The setup API: what the browser setup application talks to.

Cookie-authenticated, under /api/setup/. Every event-scoped route names
`EventAdmin` and gets the request's connection with the check already made;
the routes that touch the feed reach it through `request.app.state`, where
web.py bound it. Route order matters inside this module: FastAPI matches in
declaration order, so a literal segment ("reorder", "move", "delete") is
declared before the parameterised route that would otherwise swallow it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import access, admin, build, categories, db, deps, feed, importer, kml, users
from . import ingest as ingest_module
from .deps import (SECURE_SESSION_COOKIE, SESSION_COOKIE, Conn, EventAdmin,
                   EventCreator, SignedIn, SystemAdmin, UserManager, json_body)

log = logging.getLogger(__name__)

router = APIRouter()

# A link's label is a note to the officer handing links out - "Dana, phone" -
# so it is trimmed and capped and never validated further. Nothing reads it but
# a human deciding which row to revoke.
MAX_LINK_LABEL = 60


def _link_label(value: object) -> str | None:
    text = str(value or "").strip()[:MAX_LINK_LABEL]
    return text or None


def _seed_event_center(conn: sqlite3.Connection, event_id: int) -> None:
    """After an import, give an event with no centre one.

    The centre is where the maps open before there is a course or a place
    to fit - and until the setup form grew a field for it, nothing but the
    CLI ever set it, so the Places map opened on the whole country and the
    first click landed in Kansas. The staged file says where the event is;
    take its middle, ONCE. A centre the club typed, or one an earlier import
    already set, is never overwritten - the club may have picked the finish
    line on purpose, and a second file (a shuttle route, a parking map) is
    not the event.
    """
    row = conn.execute(
        "SELECT center_lat, center_lon FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None or (row["center_lat"] is not None
                       and row["center_lon"] is not None):
        return
    center = importer.suggest_event_center(conn, event_id)
    if center is None:
        return
    lon, lat = center      # geo speaks (lon, lat); the table stores lat, lon
    conn.execute(
        "UPDATE event SET center_lat = ?, center_lon = ? WHERE id = ?",
        (lat, lon, event_id),
    )


def request_is_secure(request: Request) -> bool:
    """Whether the browser reached us over HTTPS.

    Behind a reverse proxy the application is spoken to in plain HTTP on
    localhost, so `request.url.scheme` is "http" no matter how the browser
    connected. Taking that at face value would mean session cookies never
    get the Secure flag in exactly the deployment where it matters.

    Uvicorn rewrites the scheme from X-Forwarded-Proto when started with
    proxy headers enabled and the proxy's address trusted, which is what
    `courseops serve --behind-proxy` does. The header is read here only
    because uvicorn has already decided the peer was allowed to set it -
    trusting it unconditionally would let any client claim HTTPS.
    """
    return request.url.scheme == "https"


def client_host(request: Request) -> str:
    """The address a request came from, for the sign-in limiter.

    Behind Apache every request arrives from 127.0.0.1; with
    `--behind-proxy` uvicorn has already replaced the peer with the
    X-Forwarded-For address, and only for a peer it was told to trust -
    so this is the real client there and the loopback address otherwise,
    and never a header any client could set.
    """
    return request.client.host if request.client else ""


def _refuse_if_throttled(request: Request, username: str) -> tuple[str, ...]:
    """The limiter keys for this attempt, or a 429 if it is over the line.

    Checked BEFORE the body is hashed: the whole point is that a flood of
    wrong passwords costs the server nothing, because every hash it does
    run takes a third of a second during which no phone in the field
    receives a position.
    """
    keys = ("user:" + users.normalize_username(username),
            "addr:" + client_host(request))
    wait = request.app.state.login_limiter.retry_after(*keys)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    return keys


def _set_session_cookie(response, token: str, secure: bool) -> None:
    response.set_cookie(
        SECURE_SESSION_COOKIE if secure else SESSION_COOKIE, token,
        httponly=True,          # unreadable from JavaScript
        samesite="lax",         # not sent on cross-site POSTs
        secure=secure,          # HTTPS only, when we are on HTTPS
        max_age=users.SESSION_DAYS * 24 * 3600,
        path="/",
    )


def _clear_session_cookie(response) -> None:
    # Both names: which one the browser holds depends on how it reached
    # us, and a sign-out that leaves the other behind is not a sign-out.
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True)


@router.get("/api/setup/session")
async def whoami(request: Request, conn: Conn) -> JSONResponse:
    user = deps.current_user(request, conn)
    first_run = not users.any_users(conn)
    return JSONResponse({
        "user": user.as_dict() if user else None,
        "first_run": first_run,
        # Which build is running. Here rather than on /healthz, which is
        # unauthenticated and deliberately says nothing beyond liveness and
        # a version: the exact commit tells anyone who asks precisely which
        # code is deployed, and that is a free gift to somebody looking for
        # a version with a known problem.
        "version": request.app.state.version,
        "build": build.build_id() if user else "",
    })


@router.post("/api/setup/first-user")
async def create_first_user(request: Request) -> JSONResponse:
    """Create the first system administrator, once.

    Open only while no users exist - after that it is closed permanently,
    so it cannot be used to add an admin to a running system.

    Does NOT start a session: the account is created and the person then
    signs in with it.
    """
    body = await json_body(request)
    username = str(body.get("username", "") or "")
    keys = _refuse_if_throttled(request, username)

    # The code is read off a screen and typed on a phone, so case and
    # surrounding space are forgiven; nothing else is. Checked before
    # the hash, and a miss counts like a wrong password, because eight
    # hex characters is a small space if guessing is free.
    offered = str(body.get("setup_code", "") or "").strip().upper()
    if not hmac.compare_digest(offered, request.app.state.setup_code):
        request.app.state.login_limiter.failed(*keys)
        raise HTTPException(
            status_code=403,
            detail="The setup code is wrong. It is printed where the "
                   "server was started (or in its log).",
        )

    def create() -> users.User:
        # Its own connection, opened in the worker thread: a sqlite
        # connection refuses to be used from any thread but the one
        # that opened it, and the hash has to run off the loop.
        conn = deps.connect(request)
        try:
            if users.any_users(conn):
                raise HTTPException(
                    status_code=409,
                    detail="Setup is already complete. Sign in instead.",
                )
            return users.create_user(
                conn, username, body.get("password", ""),
                users.ROLE_SYSTEM_ADMIN, body.get("display_name"),
            )
        finally:
            conn.close()

    try:
        user = await asyncio.to_thread(create)
    except users.AuthError as exc:
        # Two submits can race: both see no users, both try to create, and
        # the loser hits the unique constraint. From the person's point of
        # view their account WAS created, so say that rather than the
        # confusing "already exists".
        check = deps.connect(request)
        try:
            exists = users.any_users(check)
        finally:
            check.close()
        if "already exists" in str(exc) and exists:
            raise HTTPException(
                status_code=409,
                detail="Setup is already complete. Sign in instead.",
            )
        # A rejected password is a failure worth counting: this route is
        # open to the whole internet until the first account exists.
        request.app.state.login_limiter.failed(*keys)
        raise HTTPException(status_code=400, detail=str(exc))
    # Deliberately no session: they sign in with the account straight away,
    # which proves the password works while they still remember typing it.
    # This is a credential they may not use again until the next event.
    return JSONResponse({"user": user.as_dict(), "created": True},
                        status_code=201)


@router.post("/api/setup/login")
async def login(request: Request) -> JSONResponse:
    """Sign an administrator in.

    The hash runs in a worker thread, on a connection opened there. It
    ran on the event loop once: a third of a second per attempt during
    which the WebSocket fan-out, the snapshots and the incident posts all
    waited - so two wrong passwords a second from anyone at all, with no
    credential, froze the map for every volunteer, and on the phones it
    looked exactly like a bad signal.
    """
    body = await json_body(request)
    username = str(body.get("username", "") or "")
    keys = _refuse_if_throttled(request, username)

    def sign_in() -> tuple[users.User, str]:
        conn = deps.connect(request)
        try:
            user = users.authenticate(conn, username, body.get("password", ""))
            return user, users.start_session(conn, user.id)
        finally:
            conn.close()

    try:
        user, token = await asyncio.to_thread(sign_in)
    except users.AuthError as exc:
        request.app.state.login_limiter.failed(*keys)
        raise HTTPException(status_code=401, detail=str(exc))
    request.app.state.login_limiter.succeeded(*keys)
    response = JSONResponse({"user": user.as_dict()})
    _set_session_cookie(response, token, request_is_secure(request))
    return response


@router.post("/api/setup/logout")
async def logout(request: Request, conn: Conn) -> JSONResponse:
    users.end_session(conn, deps.session_token(request))
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


@router.post("/api/setup/password")
async def change_own_password(
    request: Request, auth: SignedIn
) -> JSONResponse:
    user = auth.user
    body = await json_body(request)
    keys = _refuse_if_throttled(request, user.username)

    def change() -> None:
        conn = deps.connect(request)
        try:
            # Re-authenticate first: a borrowed unlocked laptop must not
            # be enough to lock the real owner out.
            users.authenticate(conn, user.username,
                               body.get("current_password", ""))
            users.set_password(conn, user.id, body.get("new_password", ""))
        finally:
            conn.close()

    try:
        await asyncio.to_thread(change)
    except users.AuthError as exc:
        request.app.state.login_limiter.failed(*keys)
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.login_limiter.succeeded(*keys)
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)   # sessions were cleared
    return response

# --- setup: events -----------------------------------------------------


def _guard(fn, *args):
    """Turn a domain error into a 400 with its message.

    The dependency that opened the connection closes it. TypeError is here
    because `int(None)` from a missing body field is one, and
    IntegrityError because a foreign key that does not exist (an
    organization id, an event id) or a NOT NULL column is the database
    saying the same thing a ValueError would - the person on the setup
    screen needs the message, not "Internal Server Error".
    """
    try:
        return fn(*args)
    except (ValueError, TypeError, users.AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"That does not fit the data already here: {exc}.")


@router.get("/api/setup/events")
async def setup_events(auth: SignedIn) -> JSONResponse:
    conn, user = auth
    if user.is_system_admin:
        events = admin.list_events(conn)
    else:
        # Scoped to the club first, so another club's race calendar is
        # never sent at all - not even to be filtered out here.
        events = admin.list_events(conn, user.organization_id)
        if not user.is_org_admin:
            allowed = set(users.events_for(conn, user.id))
            events = [e for e in events if e["id"] in allowed]
    organizations = (users.list_organizations(conn)
                     if user.is_system_admin else [])
    return JSONResponse({"events": events, "organizations": organizations})


@router.post("/api/setup/events")
async def setup_create_event(
    auth: EventCreator, request: Request
) -> JSONResponse:
    conn, user = auth
    body = await json_body(request)
    # A system admin says which club; anyone else gets their own, so a
    # club admin cannot create an event inside someone else's.
    if user.is_system_admin:
        organization_id = body.get("organization_id")
        if not organization_id:
            raise HTTPException(
                status_code=400,
                detail="Choose which organization this event belongs to.",
            )
    else:
        organization_id = user.organization_id
    event = _guard(_create_event, conn, body, organization_id)
    return JSONResponse(event, status_code=201)


def _create_event(conn, body: dict, organization_id) -> dict:
    # Inside the guard, so a non-numeric or unknown organization id is a
    # message rather than a traceback.
    return admin.create_event(conn, body, _int(organization_id, "organization"))


def _int(value, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a {what} id.") from None


@router.post("/api/setup/events/{event_id}")
async def setup_update_event(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    event = _guard(admin.update_event, conn, event_id, body)
    return JSONResponse(event)


@router.post("/api/setup/events/{event_id}/delete")
async def setup_delete_event(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    # Deleting destroys the whole history and cascades through courses,
    # roster, positions and incidents - so it needs more than event-level
    # access, but a club must still be able to remove its own events.
    conn, user = auth
    if not user.may_create_events:
        raise HTTPException(
            status_code=403,
            detail="Only an organization or system administrator can delete an event.",
        )
    row = conn.execute(
        "SELECT slug FROM event WHERE id = ?", (event_id,)).fetchone()
    admin.delete_event(conn, event_id)
    # The feed must not outlive the event - see feed.forget_ingest.
    if row is not None:
        await request.app.state.forget_ingest(row["slug"], event_id)
    return JSONResponse({"deleted": event_id})

# --- setup: course import ----------------------------------------------


@router.post("/api/setup/events/{event_id}/import")
async def setup_import(
    event_id: int, auth: EventAdmin, file: UploadFile = File(...)
) -> JSONResponse:
    conn = auth.conn
    # Written to a temp file because the parser takes a path: it has to
    # detect KMZ by reading the zip header, not by trusting the extension.
    # Streamed there in chunks rather than read into memory first - a
    # 64 MB file is within the parser's limit and does not need to be
    # held in RAM as well as on disk - and counted on the way, because
    # the Content-Length check above is only as honest as the client.
    # TemporaryDirectory removes the directory too; mkdtemp left one
    # empty directory behind per upload for the life of the service.
    lowered = (file.filename or "").lower()
    suffix = next((ext for ext in (".kmz", ".gpx") if lowered.endswith(ext)), ".kml")
    with tempfile.TemporaryDirectory() as workdir:
        tmp = Path(workdir) / f"upload{suffix}"
        written = 0
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > kml.MAX_KML_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than the "
                               f"{kml.MAX_KML_BYTES / 1e6:.0f} MB limit.")
                out.write(chunk)
        try:
            # Off the loop: parsing a 1200-point KMZ and measuring
            # every segment takes long enough that positions would
            # visibly stall for everyone if the course were
            # re-imported during the event.
            summary = await asyncio.to_thread(
                importer.stage_file, conn, event_id, tmp)
        except (kml.KmlError, zipfile.BadZipFile) as exc:
            # BadZipFile: a truncated KMZ passes is_zipfile and fails
            # inside the reader, which was a 500 with a traceback in
            # the journal rather than a sentence on the screen.
            raise HTTPException(status_code=400, detail=str(exc))
    _seed_event_center(conn, event_id)
    result = {
        "filename": file.filename,
        "total": summary.total,
        "by_type": summary.by_type,
        "warnings": summary.warnings,
        "features": admin.staged_features(conn, event_id),
    }
    return JSONResponse(result, status_code=201)


@router.get("/api/setup/events/{event_id}/staged")
async def setup_staged(event_id: int, auth: EventAdmin) -> JSONResponse:
    conn = auth.conn
    return JSONResponse({"features": admin.staged_features(conn, event_id)})


@router.post("/api/setup/events/{event_id}/assign")
async def setup_assign(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    result = _guard(admin.assign_features, conn, event_id, body)
    return JSONResponse(result)

# --- setup: courses and aid stations -----------------------------------


@router.get("/api/setup/events/{event_id}/courses")
async def setup_courses(event_id: int, auth: EventAdmin) -> JSONResponse:
    conn = auth.conn
    return JSONResponse({
        "courses": admin.list_courses(conn, event_id),
        "pois": admin.list_pois(conn, event_id),
    })


# Literal before parameterised, or "reorder" parses as a course id.
@router.post("/api/setup/events/{event_id}/courses/reorder")
async def setup_reorder_courses(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    count = _guard(
        admin.reorder_courses, conn, event_id, body.get("course_ids") or [])
    return JSONResponse({"ordered": count})


@router.post("/api/setup/events/{event_id}/courses/{course_id}")
async def setup_update_course(
    event_id: int, course_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    return JSONResponse(
        _guard(admin.update_course, conn, event_id, course_id, body)
    )


@router.post("/api/setup/events/{event_id}/courses/{course_id}/delete")
async def setup_delete_course(
    event_id: int, course_id: int, auth: EventAdmin
) -> JSONResponse:
    conn = auth.conn
    blocked = admin.delete_course(conn, event_id, course_id)
    if blocked:
        # The reports would cascade away with nothing to say where they
        # went - the same refusal as deleting a sighted leader.
        raise HTTPException(
            status_code=409,
            detail=f"{blocked} recorded on this course. Clear them first.")
    return JSONResponse({"deleted": course_id})


# Declared before /pois/{poi_id}: FastAPI matches in declaration order,
# so with the parameterised route first this one is never reached - the
# word "move" gets parsed as a poi_id and the request 422s. The failure
# is quiet in the UI, which just does nothing.
# Literal before parameterised, for the same reason as /pois/move below.
# Add a place by hand, for the organizer who supplies no water stops - or
# none at all, as a parade or a vehicle race will not.
@router.post("/api/setup/events/{event_id}/pois")
async def setup_add_poi(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    row = _guard(admin.create_poi, conn, event_id, body)
    return JSONResponse(row, status_code=201)


@router.post("/api/setup/events/{event_id}/pois/reorder")
async def setup_reorder_pois(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    count = _guard(
        admin.reorder_pois, conn, event_id, body.get("poi_ids") or [])
    return JSONResponse({"ordered": count})


@router.post("/api/setup/events/{event_id}/pois/move")
async def setup_move_pois(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    moved = _guard(
        admin.move_pois, conn, event_id,
        body.get("poi_ids") or [], (body.get("poi_type") or "").strip(),
    )
    return JSONResponse({"moved": moved})


@router.post("/api/setup/events/{event_id}/pois/{poi_id}")
async def setup_update_poi(
    event_id: int, poi_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    return JSONResponse(
        _guard(admin.update_poi, conn, event_id, poi_id, body)
    )


@router.post("/api/setup/events/{event_id}/pois/{poi_id}/delete")
async def setup_delete_poi(
    event_id: int, poi_id: int, auth: EventAdmin
) -> JSONResponse:
    conn = auth.conn
    blocked = admin.delete_poi(conn, event_id, poi_id)
    if blocked:
        # Sightings would cascade away and the posted operator would fall
        # off the map, neither with anything on screen to say why.
        raise HTTPException(
            status_code=409,
            detail=f"{blocked} at this place. Clear the sightings and "
                   "move the stations first.")
    return JSONResponse({"deleted": poi_id})

# --- setup: roster ------------------------------------------------------


@router.get("/api/setup/events/{event_id}/roster")
async def setup_roster(event_id: int, auth: EventAdmin) -> JSONResponse:
    conn = auth.conn
    return JSONResponse({
        "roster": admin.list_roster(conn, event_id),
        "categories": [
            {"key": row["key"], "name": row["name"]}
            for row in categories.roster_roles(conn, event_id)
        ],
        # Only places we staff can have somebody posted to them.
        # Offering a portable toilet or a mile marker here would be
        # noise, and the list is long enough already.
        "pois": [
            poi for poi in admin.list_pois(conn, event_id)
            if poi["poi_type"] in categories.staffed_keys(conn, event_id)
        ],
        "ignored": sorted(db.excluded_station_keys(conn, event_id)),
    })


@router.get("/api/setup/events/{event_id}/tracking")
async def setup_tracking(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    return JSONResponse(
        _guard(feed.tracking_state, request.app, conn, event_id,
               str(request.base_url)))


@router.post("/api/setup/events/{event_id}/tracking/phone")
async def setup_set_phone_tracking(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    """Turn phone tracking on, reset its URL, or turn it off.

    One token for the whole event, so "reset" and "off" cut off every
    phone at once - the accepted trade for a one-day event, recorded in
    docs/phone-tracking.md. "on" is a no-op while a token exists: a
    double press must not silently change the URL on the printed card.
    """
    conn = auth.conn
    body = await json_body(request)
    action = str(body.get("action", ""))
    current = db.tracker_token(conn, event_id)
    if action == "on":
        if not current:
            db.set_tracker_token(conn, event_id, access.generate_token())
    elif action == "reset":
        db.set_tracker_token(conn, event_id, access.generate_token())
    elif action == "off":
        db.set_tracker_token(conn, event_id, None)
    else:
        raise HTTPException(status_code=400,
                            detail="action must be on, reset or off.")
    return JSONResponse(
        _guard(feed.tracking_state, request.app, conn, event_id,
               str(request.base_url)))


@router.post("/api/setup/events/{event_id}/tracking")
async def setup_set_tracking(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    """Turn this event's APRS-IS feed on or off.

    Off outside race day is the intended state, not an oversight: the
    filter matches each operator's callsign wherever they are, so a feed
    left running logs where volunteers live and work for as long as it is
    up. See docs/PLAN.md.
    """
    conn = auth.conn
    body = await json_body(request)
    wanted = bool(body.get("enabled"))
    state = _guard(feed.tracking_state, request.app, conn, event_id)
    event = conn.execute(
        "SELECT slug, aprs_filter_extra FROM event WHERE id = ?",
        (event_id,),
    ).fetchone()
    slug = event["slug"]

    # Refuse rather than start a task that dies immediately: the
    # switch would sit at "on" with nothing behind it. Both refusals
    # mirror the feed's own, so the switch never asks for something
    # the feed will turn down. The messages say what to do about it.
    if wanted and not state["has_callsign"]:
        raise HTTPException(
            status_code=400,
            detail=" ".join(
                state["callsign_problem"].split()))
    if (wanted and state["tracked"] == 0 and state["area_mi"] is None
            and not event["aprs_filter_extra"]):
        raise HTTPException(
            status_code=400, detail=ingest_module.NOTHING_TO_LISTEN_FOR)
    if not wanted:
        db.set_ingest_enabled(conn, slug, False)
        await request.app.state.stop_ingest(slug)
    else:
        # Start first, persist second. The flag is what the next boot
        # acts on, so it must describe a feed that actually started: a
        # flag written before the attempt turned one refused press into
        # a service that restarted into the same failure under systemd.
        started = await request.app.state.start_ingest(slug)
        if not started:
            raise HTTPException(
                status_code=400,
                detail=request.app.state.ingest_errors.get(slug)
                or "The feed stopped before it connected.")
        db.set_ingest_enabled(conn, slug, True)

    return JSONResponse(feed.tracking_state(request.app, conn, event_id,
                                            str(request.base_url)))


@router.get("/api/setup/events/{event_id}/categories")
async def setup_categories(
    event_id: int, auth: EventAdmin
) -> JSONResponse:
    conn = auth.conn
    # The counts are what make "delete" honest: a layer with places,
    # a role someone holds, a leader with sightings cannot go, and
    # the number says how many are in the way.
    places = categories.place_counts(conn, event_id)
    roles = categories.role_counts(conn, event_id)
    sightings = categories.sighting_counts(conn, event_id)
    payload = {
        "poi_categories": [
            dict(row) | {"place_count": places.get(row["key"], 0)}
            for row in categories.poi_categories(conn, event_id)
        ],
        "roster_roles": [
            dict(row) | {"in_use": roles.get(row["key"], 0)}
            for row in categories.roster_roles(conn, event_id)
        ],
        "lead_divisions": [
            dict(row) | {"in_use": sightings.get(row["key"], 0)}
            for row in categories.lead_divisions(conn, event_id)
        ],
    }
    return JSONResponse(payload)


# --- setup: the taxonomies -----------------------------------------------
#
# Place layers, station roles and leaders are the same kind of thing: a
# list the club owns, keyed once and named freely, with add / reorder /
# rename / delete - and "delete" refused with a count while something
# still uses the key. Three copies of those four routes had already
# drifted once (one refused with 409, two with 400, until the audit
# unified them), and the rule that a fourth taxonomy "goes the same way"
# was sixty more lines of copy. One table now; the routes are made from
# it, four per taxonomy, each with its LITERAL path - never `/{kind}/...`,
# which would swallow /roster, /tracking and /links behind it.

@dataclass(frozen=True)
class Taxonomy:
    """One club-owned list and the domain functions behind its routes."""
    segment: str
    add: Callable[[sqlite3.Connection, int, dict], sqlite3.Row]
    update: Callable[[sqlite3.Connection, int, str, dict], sqlite3.Row]
    delete: Callable[[sqlite3.Connection, int, str], int]   # -> still in use
    in_use: Callable[[int], str]                             # the 409 detail
    reorder: Callable[[sqlite3.Connection, int, list], int] | None = None


TAXONOMIES = (
    Taxonomy(
        segment="categories",
        add=lambda conn, event_id, body: categories.add_poi_category(
            conn, event_id, body.get("name", ""), bool(body.get("staffed")),
            body.get("icon") or "pin", body.get("color")),
        update=categories.update_poi_category,
        reorder=categories.reorder_poi_categories,
        delete=categories.delete_poi_category,
        # Deleting the layer would leave its places drawn in no layer at
        # all - present in the database, invisible on the map, no error.
        in_use=lambda n: (f"{n} place(s) still use this layer. "
                          "Move or delete them first."),
    ),
    Taxonomy(
        segment="roles",
        add=lambda conn, event_id, body: categories.add_roster_role(
            conn, event_id, body.get("name") or ""),
        update=lambda conn, event_id, key, body: categories.rename_roster_role(
            conn, event_id, key, body.get("name", "")),
        delete=categories.delete_roster_role,
        in_use=lambda n: (f"{n} roster entr{'y' if n == 1 else 'ies'} "
                          "still use this role. Move them first."),
    ),
    # The leaders this event tracks - "First male", "First wheelchair".
    # Called leaders on screen and in these routes; the key stored on a
    # sighting is still `division`, which is internal and in databases
    # that already exist.
    Taxonomy(
        segment="leaders",
        add=lambda conn, event_id, body: categories.add_lead_division(
            conn, event_id, body.get("name") or ""),
        update=lambda conn, event_id, key, body: categories.rename_lead_division(
            conn, event_id, key, body.get("name", "")),
        reorder=categories.reorder_lead_divisions,
        delete=categories.delete_lead_division,
        # The sightings would stay in the database and vanish from the
        # panel, with nothing on screen to say where they went.
        in_use=lambda n: (f"{n} sighting{'' if n == 1 else 's'} "
                          "recorded against this leader. Clear them first."),
    ),
)


def _taxonomy_routes(taxonomy: Taxonomy) -> None:
    """Register add / reorder / delete / rename for one taxonomy.

    Declared in this order on purpose: "reorder" is a literal and would
    otherwise parse as a key.
    """
    base = f"/api/setup/events/{{event_id}}/{taxonomy.segment}"

    @router.post(base, name=f"setup_add_{taxonomy.segment}")
    async def add(event_id: int, auth: EventAdmin,
                  request: Request) -> JSONResponse:
        body = await json_body(request)
        row = _guard(taxonomy.add, auth.conn, event_id, body)
        return JSONResponse(dict(row), status_code=201)

    if taxonomy.reorder is not None:
        @router.post(base + "/reorder", name=f"setup_reorder_{taxonomy.segment}")
        async def reorder(event_id: int, auth: EventAdmin,
                          request: Request) -> JSONResponse:
            body = await json_body(request)
            count = _guard(taxonomy.reorder, auth.conn, event_id,
                           body.get("keys") or [])
            return JSONResponse({"ordered": count})

    @router.post(base + "/{key}/delete", name=f"setup_delete_{taxonomy.segment}")
    async def delete(event_id: int, key: str, auth: EventAdmin) -> JSONResponse:
        in_use = _guard(taxonomy.delete, auth.conn, event_id, key)
        if in_use:
            # 409, not 400: the request was well formed, it is the data
            # that is in the way - and the count says how much of it.
            raise HTTPException(status_code=409, detail=taxonomy.in_use(in_use))
        return JSONResponse({"deleted": key})

    @router.post(base + "/{key}", name=f"setup_update_{taxonomy.segment}")
    async def update(event_id: int, key: str, auth: EventAdmin,
                     request: Request) -> JSONResponse:
        body = await json_body(request)
        row = _guard(taxonomy.update, auth.conn, event_id, key, body)
        return JSONResponse(dict(row))


for _taxonomy in TAXONOMIES:
    _taxonomy_routes(_taxonomy)



@router.post("/api/setup/events/{event_id}/roster")
async def setup_save_roster(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    return JSONResponse(
        _guard(admin.save_roster_entry, conn, event_id, body)
    )


@router.post("/api/setup/events/{event_id}/roster/delete")
async def setup_delete_roster(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    _guard(admin.delete_roster_entry, conn, event_id,
           body.get("station_key", ""))
    return JSONResponse({"ok": True})

# --- setup: access links ------------------------------------------------


@router.get("/api/setup/events/{event_id}/links")
async def setup_links(event_id: int, auth: EventAdmin) -> JSONResponse:
    conn = auth.conn
    # A read, and only a read. This used to create any role's
    # missing link on the way past, which is benign in itself - but
    # Lax cookies ARE sent on a cross-site top-level navigation, so
    # a GET with a side effect is the one kind of setup route a page
    # elsewhere can drive. The fill-in lives on the POST below.
    event = conn.execute(
        "SELECT slug FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    return JSONResponse({
        "slug": event["slug"],
        "links": admin.list_links(conn, event_id),
    })


@router.post("/api/setup/events/{event_id}/links")
async def setup_link_action(
    event_id: int, auth: EventAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    action = (body.get("action") or "").strip()

    def token_id() -> int:
        try:
            return int(body.get("token_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Which link?")

    if action == "revoke":
        # 404, not 400: the id is either not a link at all or is a
        # link in some other event, and the difference must not be
        # reported - it would confirm the other event's link exists.
        if not access.revoke(conn, event_id, token_id()):
            raise HTTPException(status_code=404, detail="No such link.")
    elif action == "add":
        # A second, third, fourth link for one role. Three Net Control
        # operators can share one link - the token allows any number of
        # devices - but then no one of them can be cut off alone, and
        # nothing says which of them is which. A link per person is how
        # a phone left in a parking lot is revoked without taking the
        # net off the air.
        role = str(body.get("role", ""))
        if role not in access.ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown role {role!r}")
        access.create_token(conn, event_id, role,
                            _link_label(body.get("label")))
    elif action == "label":
        # Whose link this is. Free text and never trusted for anything:
        # it exists so the row to revoke can be found under pressure.
        if not access.set_label(conn, event_id, token_id(),
                                _link_label(body.get("label"))):
            raise HTTPException(status_code=404, detail="No such link.")
    elif action == "reissue":
        role = str(body.get("role", ""))
        if role not in access.ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown role {role!r}")
        # Revoke the old one in the same step: reissuing without
        # revoking would quietly leave the leaked link working. With
        # several links on a role this replaces ALL of them, which is
        # what "the role is compromised" means - revoking one person is
        # the per-link action above.
        for row in access.tokens_for_event(conn, event_id):
            if row["role"] == role and not row["revoked"]:
                access.revoke(conn, event_id, row["id"])
        access.create_token(conn, event_id, role)
    else:
        raise HTTPException(status_code=400, detail="Unknown action.")
    # Every role keeps at least one live link: revoking the only NCS
    # link is a rotation, not a net with no Net Control. Fills in a
    # missing role and never collapses extras.
    access.ensure_tokens(conn, event_id)
    links = admin.list_links(conn, event_id)
    return JSONResponse({"links": links})

# --- setup: organizations -----------------------------------------------


@router.get("/api/setup/organizations")
async def setup_organizations(auth: SignedIn) -> JSONResponse:
    conn, user = auth
    organizations = users.list_organizations(conn)
    if not user.is_system_admin:
        organizations = [o for o in organizations
                         if o["id"] == user.organization_id]
    return JSONResponse({"organizations": organizations})


@router.post("/api/setup/organizations")
async def setup_create_organization(
    auth: SystemAdmin, request: Request
) -> JSONResponse:
    # Only the host adds clubs: this is the tenancy boundary itself.
    conn = auth.conn
    body = await json_body(request)
    organization = _guard(
        users.create_organization, conn,
        body.get("slug", ""), body.get("name", ""), body.get("contact"),
    )
    return JSONResponse(organization, status_code=201)


@router.post("/api/setup/organizations/{organization_id}")
async def setup_update_organization(
    organization_id: int, auth: SystemAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    body = await json_body(request)
    organization = _guard(
        users.update_organization, conn, organization_id, body
    )
    return JSONResponse(organization)


@router.post("/api/setup/organizations/{organization_id}/delete")
async def setup_delete_organization(
    organization_id: int, auth: SystemAdmin, request: Request
) -> JSONResponse:
    conn = auth.conn
    # Cascades through the organization's events, so their feeds go
    # the same way an event's own delete takes its feed with it.
    gone = conn.execute(
        "SELECT id, slug FROM event WHERE organization_id = ?",
        (organization_id,)).fetchall()
    conn.execute("DELETE FROM organization WHERE id = ?", (organization_id,))
    for event in gone:
        await request.app.state.forget_ingest(event["slug"], event["id"])
    return JSONResponse({"deleted": organization_id})

# --- setup: users -------------------------------------------------------


@router.get("/api/setup/users")
async def setup_users(auth: UserManager) -> JSONResponse:
    conn, user = auth
    people = users.list_users(conn)
    roles = list(users.ROLES)
    if not user.is_system_admin:
        # An org admin sees and creates only within their own club, and
        # cannot mint system administrators.
        people = [p for p in people
                  if p["organization_id"] == user.organization_id]
        roles = [r for r in roles if r != users.ROLE_SYSTEM_ADMIN]
    return JSONResponse({
        "users": people,
        "roles": [{"value": r, "label": users.ROLE_LABELS[r]} for r in roles],
        "organizations": (users.list_organizations(conn)
                          if user.is_system_admin else []),
    })


@router.post("/api/setup/users")
async def setup_create_user(
    auth: UserManager, request: Request
) -> JSONResponse:
    conn, actor = auth
    body = await json_body(request)
    role = body.get("role", "")
    if actor.is_system_admin:
        organization_id = body.get("organization_id")
    else:
        # Their own club, always - and never a system administrator.
        organization_id = actor.organization_id
        if role == users.ROLE_SYSTEM_ADMIN:
            raise HTTPException(
                status_code=403,
                detail="Only a system administrator can create one.",
            )
    created = await asyncio.to_thread(
        _guard, _create_user, request, body, role, organization_id)
    return JSONResponse(created.as_dict(), status_code=201)


def _create_user(request: Request, body: dict, role, organization_id
                 ) -> users.User:
    # Runs in a worker thread, like login: the hash is a third of a second
    # during which nothing else on the loop moves, and a manager adding an
    # account on race morning was freezing every phone for that long. Its
    # own connection, opened here, because a sqlite connection refuses any
    # thread but the one that opened it.
    conn = deps.connect(request)
    try:
        # Everything checked before the INSERT: the connection is
        # autocommit, so validating event_ids after create_user left a
        # half-made account behind the error.
        org = (_int(organization_id, "organization")
               if organization_id else None)
        event_ids = _event_ids(conn, body.get("event_ids", []), org)
        with db.transaction(conn):
            created = users.create_user(
                conn, body.get("username", ""), body.get("password", ""),
                role, body.get("display_name"), org,
            )
            users.set_events(conn, created.id, event_ids)
        return created
    finally:
        conn.close()


def _event_ids(conn, values, organization_id) -> list[int]:
    """Event ids for an administrator's assignment, all real, all theirs.

    `may_access_event` checks the organization before the assignment, so
    a cross-club row granted nothing - but it was a junk row, and an id
    that did not exist was a foreign-key traceback after the account had
    already been created.
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("event_ids must be a list.")
    ids = [_int(v, "event") for v in values]
    allowed = {e["id"] for e in admin.list_events(conn, organization_id)}
    unknown = [i for i in ids if i not in allowed]
    if unknown:
        raise ValueError(
            f"No event with id {unknown[0]} in this organization.")
    return ids


@router.post("/api/setup/users/{user_id}")
async def setup_update_user(
    user_id: int, auth: UserManager, request: Request
) -> JSONResponse:
    conn, actor = auth
    body = await json_body(request)
    target = _get_user(conn, user_id)
    if not users.may_manage_user(conn, actor, target):
        raise HTTPException(status_code=403, detail="Not your administrator.")
    if "password" in body:
        def reset() -> None:
            # Off the loop, on its own connection - see _create_user.
            own = deps.connect(request)
            try:
                _guard(users.set_password, own, user_id, body["password"])
            finally:
                own.close()
        await asyncio.to_thread(reset)
    if "is_active" in body:
        active = bool(body["is_active"])
        # Refuse to deactivate the last system admin: it would lock
        # everyone out of the system with no way back in.
        if not active and users.count_system_admins(conn, user_id) == 0:
            raise HTTPException(
                status_code=400,
                detail="This is the only system administrator.",
            )
        users.set_active(conn, user_id, active)
    if "event_ids" in body:
        users.set_events(conn, user_id, _guard(
            _event_ids, conn, body["event_ids"], target.organization_id))
    result = users.get_user(conn, user_id).as_dict()
    return JSONResponse(result)


def _get_user(conn, user_id: int) -> users.User:
    # Two admins editing the same list - one deletes, the other saves -
    # is a 404 with a message, not a blank error.
    try:
        return users.get_user(conn, user_id)
    except users.AuthError:
        raise HTTPException(status_code=404, detail="No such administrator.")


@router.post("/api/setup/users/{user_id}/delete")
async def setup_delete_user(
    user_id: int, auth: UserManager
) -> JSONResponse:
    conn, actor = auth
    target = _get_user(conn, user_id)
    if not users.may_manage_user(conn, actor, target):
        raise HTTPException(status_code=403, detail="Not your administrator.")
    if user_id == actor.id:
        raise HTTPException(
            status_code=400, detail="You cannot delete your own account."
        )
    if users.count_system_admins(conn, user_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="This is the only system administrator.",
        )
    users.delete_user(conn, user_id)
    return JSONResponse({"deleted": user_id})

