"""The web server: state snapshot, live WebSocket feed, and the map page.

Access is by role token in the URL path. There is no public view — every route
below resolves a token or returns 404. 404 rather than 403 is deliberate: an
invalid token should not confirm that an event exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import pathlib
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (access, admin, categories, db, hub as hub_module, importer,
               incidents, resources,
               kml, leaders, progress, symbols, users)
from .config import Settings
from .ingest import run_ingest

log = logging.getLogger(__name__)

STATIC_DIR = resources.package_file("static")

SESSION_COOKIE = "courseops_session"

# Appended to local script and stylesheet URLs so a changed file is fetched
# rather than served from cache. Without it a browser runs yesterday's
# JavaScript against today's markup, producing an interface that contradicts
# itself with no error anywhere to explain it.
#
# The marker is the file's modification time, not the release version: it has
# to change the moment a file is edited, and development is when files change
# most. Icons are deliberately left unversioned - they are stable for the life
# of the app, and a changing favicon URL makes a tab look like a different site.
_ASSET_URL = re.compile(r'((?:src|href)=")(/static/[^"?]+\.(?:js|css))"')


def _asset_version(name: str) -> str:
    try:
        return str(int((STATIC_DIR / Path(name).name).stat().st_mtime))
    except OSError:
        return "0"


def _page(html: str) -> HTMLResponse:
    """Serve a page with versioned assets and uncached HTML.

    The HTML must revalidate every time, because it carries the version
    markers; if it were cached they would be too, and nothing would update.
    """
    html = _ASSET_URL.sub(
        lambda m: f'{m.group(1)}{m.group(2)}?v={_asset_version(m.group(2))}"',
        html,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})

# How long without a packet before a station is styled as going stale / silent.
# Phone apps beacon every 1-5 minutes, so "quiet for 4 minutes" is normal and
# must not read as an alarm.
STALE_AFTER_SECONDS = 10 * 60
SILENT_AFTER_SECONDS = 20 * 60


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _course_position(index: "progress.CourseIndex", lat: float, lon: float):
    located = index.locate(lat, lon)
    return located.as_dict() if located else None


def _ssid_alerts(conn: sqlite3.Connection, event_id: int) -> list[dict[str, Any]]:
    """Callsigns transmitting on an SSID the roster does not name.

    Each alert carries the roster entries for the same callsign, so the client
    can offer "this is really Aid 3" without a second round trip.
    """
    alerts = []
    for row in db.unexpected_ssids(conn, event_id):
        base = row["station_key"].split("-", 1)[0]
        candidates = db.roster_entries_for_base(conn, event_id, base)
        alerts.append({
            "station_key": row["station_key"],
            "packets": row["packets"],
            "last_at": row["last_at"],
            "symbol": symbols.describe(row["symbol_table"], row["symbol_code"]),
            # A digipeater or igate under a rostered callsign is almost always
            # infrastructure to dismiss, not a person to adopt.
            "looks_like_infrastructure": symbols.is_infrastructure(
                row["symbol_table"], row["symbol_code"]
            ),
            "roster_candidates": [
                {"station_key": entry["station_key"],
                 "display_label": entry["display_label"],
                 "category": entry["category"]}
                for entry in candidates
            ],
        })
    return alerts


def build_state(conn: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Everything a client needs to draw the map from scratch.

    Clients call this on connect AND on every reconnect, rather than trying to
    replay missed messages. A phone coming out of a dead zone gets a correct
    picture instead of a plausible-looking stale one.
    """
    event = conn.execute(
        "SELECT * FROM event WHERE id = ?", (event_id,)
    ).fetchone()

    courses = [
        {
            "id": row["id"],
            "name": row["name"],
            "color": row["color"],
            "dash_pattern": row["dash_pattern"],
            "distance_m": row["distance_m"],
            "sort_order": row["sort_order"],
            "geojson": json.loads(row["geojson"]),
        }
        for row in conn.execute(
            "SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id",
            (event_id,),
        ).fetchall()
    ]

    index = progress.CourseIndex.for_event(conn, event_id)

    # Aid stations are listed in COURSE order, not by name: see
    # CourseIndex.order_along_course for why name ordering cannot work.
    poi_rows = conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()
    pois = []
    for row in index.order_along_course(poi_rows):
        entry = _row_to_dict(row)
        entry["course_position"] = _course_position(index, row["lat"], row["lon"])
        pois.append(entry)

    roster = []
    for row in conn.execute(
        "SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label",
        (event_id,),
    ).fetchall():
        entry = _row_to_dict(row)
        # Wording differs by category: an aid station is "Torn down", a sweep is
        # "Finished". The client should not have to know that mapping.
        entry["op_status_label"] = db.op_status_label(row["category"], row["op_status"])
        # What this station's packets arrive under. Equal to station_key unless
        # the roster named a bare callsign and an SSID has been heard for it.
        # The client joins positions on this; writes still name station_key.
        entry["tracking_key"] = db.tracking_key(row)
        # An operator posted at an aid station inherits that station's place on
        # the course, so the roster can be read in course order too.
        if row["poi_id"] is not None:
            poi = next((p for p in pois if p["id"] == row["poi_id"]), None)
            if poi is not None:
                entry["course_position"] = poi["course_position"]
                entry["poi_name"] = poi["name"]
        roster.append(entry)

    # Ignoring an SSID has to hide what was already stored, not merely stop
    # future packets. Otherwise "ignore the digipeater" leaves the digipeater
    # sitting on the map, which is not what the word promises.
    ignored = db.excluded_station_keys(conn, event_id)

    positions = [
        {
            "station_key": row["station_key"],
            "received_at": row["received_at"],
            "lat": row["lat"],
            "lon": row["lon"],
            "course_deg": row["course_deg"],
            "speed_kmh": row["speed_kmh"],
            "altitude_m": row["altitude_m"],
            "symbol_table": row["symbol_table"],
            "symbol_code": row["symbol_code"],
            "comment": row["comment"],
            "course_position": _course_position(index, row["lat"], row["lon"]),
        }
        for row in db.latest_position_per_station(conn, event_id)
        if row["station_key"] not in ignored
    ]

    incident_rows = []
    for row in incidents.for_event(conn, event_id):
        entry = incidents.Incident(row).as_dict()
        # "bib 1432, mile 9.1 of Half" is dramatically more actionable over a
        # radio net than a lat/lon.
        entry["course_position"] = _course_position(index, row["lat"], row["lon"])
        incident_rows.append(entry)

    return {
        "type": "state",
        "event": {
            "slug": event["slug"],
            "name": event["name"],
            "timezone": event["timezone"],
            "center_lat": event["center_lat"],
            "center_lon": event["center_lon"],
            "zoom": event["zoom"],
        },
        "courses": courses,
        # A club's own wording for the station roles. The keys are fixed
        # because each carries its own status vocabulary; the names are not.
        "role_labels": categories.role_labels(conn, event_id),
        # A club's own wording for the station roles. The keys are fixed
        # because each carries its own status vocabulary; the names are not.
        "role_labels": categories.role_labels(conn, event_id),
        # The layers this event has, and how each draws. Sent rather than
        # assumed, because the set is the club's, not the code's.
        "poi_categories": [
            {
                "key": row["key"],
                "name": row["name"],
                "staffed": bool(row["staffed"]),
                "icon": row["icon"],
                "color": row["color"],
                "visible": bool(row["visible"]),
            }
            for row in categories.poi_categories(conn, event_id)
        ],
        "pois": pois,
        "roster": roster,
        "positions": positions,
        # Surfaced in the UI rather than left to a command someone has to
        # remember: the failure this catches is silent, and a check that must be
        # remembered will be forgotten.
        "ssid_alerts": _ssid_alerts(conn, event_id),
        "leaders": [entry.as_dict() for entry in
                    leaders.for_event(conn, event_id, index)],
        "divisions": [
            {"value": value, "label": leaders.division_label(value)}
            for value in leaders.DIVISIONS
        ],
        "incidents": incident_rows,
        "incident_statuses": [
            {"value": value, "label": incidents.STATUS_LABELS[value]}
            for value in incidents.STATUSES
        ],
        "incident_kinds": [
            {"value": value, "label": incidents.KIND_LABELS[value]}
            for value in incidents.KINDS
        ],
        # The number that means "still waiting". Notes are excluded by
        # construction - see incidents.waiting_count.
        "pickups_waiting": incidents.waiting_count(conn, event_id),
        "op_statuses": list(db.OP_STATUSES),
        "thresholds": {
            "stale_after_s": STALE_AFTER_SECONDS,
            "silent_after_s": SILENT_AFTER_SECONDS,
        },
    }


def create_app(settings: Settings, ingest_events: list[str] | None = None) -> FastAPI:
    """Build the application.

    `ingest_events` names event slugs to ingest live. Empty (the default in
    tests) means no APRS-IS connection is opened at all.
    """
    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI):
        for slug in ingest_events or []:
            task = asyncio.create_task(_ingest_for(slug), name=f"ingest:{slug}")
            application.state.ingest_tasks.append(task)
        try:
            yield
        finally:
            for task in application.state.ingest_tasks:
                task.cancel()
            for task in application.state.ingest_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Course Ops", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.hub = hub_module.Hub()
    app.state.ingest_tasks = []

    # A setup change during an event has to reach the field, not wait for
    # someone to pull to refresh.
    #
    # The failure this prevents is silent and one-sided: NCS renames a station
    # mid-event, watches it change on their own screen, and reasonably assumes
    # everyone has it - while every phone in the field still shows the old name
    # and nobody has any reason to doubt what they are reading. Renaming
    # stations mid-event is exactly what happens when the net discovers two
    # teams are using different words for the same corner.
    #
    # Done here rather than in each endpoint on purpose. There are a dozen ways
    # to change what the map shows and there will be more; one place cannot be
    # forgotten, and a new setup endpoint gets this for free.
    _SETUP_EVENT_PATH = re.compile(r"^/api/setup/events/(\d+)(?:/|$)")

    @app.middleware("http")
    async def publish_setup_changes(request: Request, call_next):
        response = await call_next(request)
        if request.method != "POST" or response.status_code >= 400:
            return response
        match = _SETUP_EVENT_PATH.match(request.url.path)
        if match:
            # A resync rather than a diff: setup edits rewrite whole sets -
            # layers, roster, courses - which no incremental message expresses,
            # and a resync cannot leave a client half-updated.
            await app.state.hub.publish(int(match.group(1)), {"type": "resync"})
        return response

    def get_conn() -> sqlite3.Connection:
        # SQLite connections are not shareable across threads; one per request
        # is cheap for this workload and avoids the whole question.
        conn = db.connect(settings.db_path)
        return conn

    def require_access(event_slug: str, token: str) -> tuple[sqlite3.Connection, access.Access]:
        conn = get_conn()
        granted = access.resolve(conn, event_slug, token)
        if granted is None:
            conn.close()
            # 404, not 403: an invalid token must not confirm the event exists.
            raise HTTPException(status_code=404, detail="Not found")
        return conn, granted

    # --- administrator sessions --------------------------------------------

    def current_user(request: Request, conn) -> users.User | None:
        return users.resolve_session(conn, request.cookies.get(SESSION_COOKIE, ""))

    def require_user(request: Request) -> tuple[Any, users.User]:
        conn = get_conn()
        user = current_user(request, conn)
        if user is None:
            conn.close()
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        return conn, user

    def require_user_manager(request: Request):
        """System admins and org admins both manage people, at different scopes."""
        conn, user = require_user(request)
        if not user.may_manage_users:
            conn.close()
            raise HTTPException(
                status_code=403, detail="You cannot manage administrators."
            )
        return conn, user

    def require_event_creator(request: Request):
        conn, user = require_user(request)
        if not user.may_create_events:
            conn.close()
            raise HTTPException(
                status_code=403, detail="You cannot create events."
            )
        return conn, user

    def require_system_admin(request: Request):
        conn, user = require_user(request)
        if not user.is_system_admin:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Only a system administrator can do that.",
            )
        return conn, user

    def require_event_admin(request: Request, event_id: int):
        """Every event-scoped setup route goes through here.

        One place decides whether a user may touch an event, so widening or
        narrowing access later is a change to may_access_event rather than to
        every endpoint.
        """
        conn, user = require_user(request)
        if not users.may_access_event(conn, user, event_id):
            conn.close()
            raise HTTPException(status_code=403, detail="Not your event.")
        return conn, user

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

    def _set_session_cookie(response, token: str, secure: bool) -> None:
        response.set_cookie(
            SESSION_COOKIE, token,
            httponly=True,          # unreadable from JavaScript
            samesite="lax",         # not sent on cross-site POSTs
            secure=secure,          # HTTPS only, when we are on HTTPS
            max_age=users.SESSION_DAYS * 24 * 3600,
            path="/",
        )

    @app.get("/setup")
    async def setup_page(request: Request) -> HTMLResponse:
        conn = get_conn()
        try:
            # First run: nobody exists yet, so the page offers to create the
            # first system administrator instead of asking for a login that
            # could never succeed.
            needs_first_user = not users.any_users(conn)
        finally:
            conn.close()
        html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8")
        return _page(
            html.replace("{{FIRST_RUN}}", "true" if needs_first_user else "false")
        )

    @app.get("/api/setup/session")
    async def whoami(request: Request) -> JSONResponse:
        conn = get_conn()
        try:
            user = current_user(request, conn)
            first_run = not users.any_users(conn)
        finally:
            conn.close()
        return JSONResponse({
            "user": user.as_dict() if user else None,
            "first_run": first_run,
        })

    @app.post("/api/setup/first-user")
    async def create_first_user(request: Request) -> JSONResponse:
        """Create the first system administrator, once.

        Open only while no users exist - after that it is closed permanently,
        so it cannot be used to add an admin to a running system.

        Does NOT start a session: the account is created and the person then
        signs in with it.
        """
        conn = get_conn()
        body = await _json_body(request, conn)
        try:
            if users.any_users(conn):
                raise HTTPException(
                    status_code=409,
                    detail="Setup is already complete. Sign in instead.",
                )
            user = users.create_user(
                conn, body.get("username", ""), body.get("password", ""),
                users.ROLE_SYSTEM_ADMIN, body.get("display_name"),
            )
        except users.AuthError as exc:
            # Two submits can race: both see no users, both try to create, and
            # the loser hits the unique constraint. From the person's point of
            # view their account WAS created, so say that rather than the
            # confusing "already exists".
            conn.close()
            check = get_conn()
            try:
                exists = users.any_users(check)
            finally:
                check.close()
            if "already exists" in str(exc) and exists:
                raise HTTPException(
                    status_code=409,
                    detail="Setup is already complete. Sign in instead.",
                )
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            if conn:
                conn.close()
        # Deliberately no session: they sign in with the account straight away,
        # which proves the password works while they still remember typing it.
        # This is a credential they may not use again until the next event.
        return JSONResponse({"user": user.as_dict(), "created": True},
                            status_code=201)

    @app.post("/api/setup/login")
    async def login(request: Request) -> JSONResponse:
        conn = get_conn()
        body = await _json_body(request, conn)
        try:
            user = users.authenticate(
                conn, body.get("username", ""), body.get("password", "")
            )
            token = users.start_session(conn, user.id)
        except users.AuthError as exc:
            conn.close()
            raise HTTPException(status_code=401, detail=str(exc))
        conn.close()
        response = JSONResponse({"user": user.as_dict()})
        _set_session_cookie(response, token, request_is_secure(request))
        return response

    @app.post("/api/setup/logout")
    async def logout(request: Request) -> JSONResponse:
        conn = get_conn()
        try:
            users.end_session(conn, request.cookies.get(SESSION_COOKIE, ""))
        finally:
            conn.close()
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/api/setup/password")
    async def change_own_password(request: Request) -> JSONResponse:
        conn, user = require_user(request)
        body = await _json_body(request, conn)
        try:
            # Re-authenticate first: a borrowed unlocked laptop must not be
            # enough to lock the real owner out.
            users.authenticate(conn, user.username, body.get("current_password", ""))
            users.set_password(conn, user.id, body.get("new_password", ""))
        except users.AuthError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")   # sessions were cleared
        return response

    # --- setup: events -----------------------------------------------------

    def _guard(fn, *args):
        """Turn a domain error into a 400 with its message, closing the conn."""
        try:
            return fn(*args)
        except (ValueError, users.AuthError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/setup/events")
    async def setup_events(request: Request) -> JSONResponse:
        conn, user = require_user(request)
        try:
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
        finally:
            conn.close()
        return JSONResponse({"events": events, "organizations": organizations})

    @app.post("/api/setup/events")
    async def setup_create_event(request: Request) -> JSONResponse:
        conn, user = require_event_creator(request)
        body = await _json_body(request, conn)
        try:
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
            event = _guard(admin.create_event, conn, body, int(organization_id))
        finally:
            conn.close()
        return JSONResponse(event, status_code=201)

    @app.post("/api/setup/events/{event_id}")
    async def setup_update_event(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            event = _guard(admin.update_event, conn, event_id, body)
        finally:
            conn.close()
        return JSONResponse(event)

    @app.post("/api/setup/events/{event_id}/delete")
    async def setup_delete_event(event_id: int, request: Request) -> JSONResponse:
        # Deleting destroys the whole history and cascades through courses,
        # roster, positions and incidents - so it needs more than event-level
        # access, but a club must still be able to remove its own events.
        conn, user = require_event_admin(request, event_id)
        if not user.may_create_events:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Only an organization or system administrator can delete an event.",
            )
        try:
            admin.delete_event(conn, event_id)
        finally:
            conn.close()
        return JSONResponse({"deleted": event_id})

    # --- setup: course import ----------------------------------------------

    @app.post("/api/setup/events/{event_id}/import")
    async def setup_import(
        event_id: int, request: Request, file: UploadFile = File(...)
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        payload = await file.read()
        # Written to a temp file because the parser takes a path: it has to
        # detect KMZ by reading the zip header, not by trusting the extension.
        import tempfile
        suffix = ".kmz" if (file.filename or "").lower().endswith(".kmz") else ".kml"
        tmp = pathlib.Path(tempfile.mkdtemp()) / f"upload{suffix}"
        tmp.write_bytes(payload)
        try:
            summary = importer.stage_file(conn, event_id, tmp)
        except kml.KmlError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            tmp.unlink(missing_ok=True)
        result = {
            "filename": file.filename,
            "total": summary.total,
            "by_type": summary.by_type,
            "warnings": summary.warnings,
            "features": admin.staged_features(conn, event_id),
        }
        conn.close()
        return JSONResponse(result, status_code=201)

    @app.get("/api/setup/events/{event_id}/staged")
    async def setup_staged(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            return JSONResponse({"features": admin.staged_features(conn, event_id)})
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/assign")
    async def setup_assign(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            result = _guard(admin.assign_features, conn, event_id, body)
        finally:
            conn.close()
        return JSONResponse(result)

    # --- setup: courses and aid stations -----------------------------------

    @app.get("/api/setup/events/{event_id}/courses")
    async def setup_courses(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            return JSONResponse({
                "courses": admin.list_courses(conn, event_id),
                "pois": admin.list_pois(conn, event_id),
            })
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/courses/{course_id}")
    async def setup_update_course(
        event_id: int, course_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            return JSONResponse(
                _guard(admin.update_course, conn, event_id, course_id, body)
            )
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/courses/{course_id}/delete")
    async def setup_delete_course(
        event_id: int, course_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            admin.delete_course(conn, event_id, course_id)
        finally:
            conn.close()
        return JSONResponse({"deleted": course_id})

    # Declared before /pois/{poi_id}: FastAPI matches in declaration order,
    # so with the parameterised route first this one is never reached - the
    # word "move" gets parsed as a poi_id and the request 422s. The failure
    # is quiet in the UI, which just does nothing.
    @app.post("/api/setup/events/{event_id}/pois/move")
    async def setup_move_pois(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            moved = _guard(
                admin.move_pois, conn, event_id,
                body.get("poi_ids") or [], (body.get("poi_type") or "").strip(),
            )
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"moved": moved})

    @app.post("/api/setup/events/{event_id}/pois/{poi_id}")
    async def setup_update_poi(
        event_id: int, poi_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            return JSONResponse(
                _guard(admin.update_poi, conn, event_id, poi_id, body)
            )
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/pois/{poi_id}/delete")
    async def setup_delete_poi(
        event_id: int, poi_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            admin.delete_poi(conn, event_id, poi_id)
        finally:
            conn.close()
        return JSONResponse({"deleted": poi_id})

    # --- setup: roster ------------------------------------------------------

    @app.get("/api/setup/events/{event_id}/roster")
    async def setup_roster(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
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
        finally:
            conn.close()

    @app.get("/api/setup/events/{event_id}/categories")
    async def setup_categories(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            payload = {
                "poi_categories": [
                    # The count is what makes "delete" honest: a layer with
                    # places in it cannot go, and the number says how many.
                    dict(row) | {"place_count": conn.execute(
                        "SELECT COUNT(*) AS c FROM poi"
                        " WHERE event_id = ? AND poi_type = ?",
                        (event_id, row["key"]),
                    ).fetchone()["c"]}
                    for row in categories.poi_categories(conn, event_id)
                ],
                "roster_roles": [
                    dict(row) for row in categories.roster_roles(conn, event_id)
                ],
            }
            conn.commit()
        finally:
            conn.close()
        return JSONResponse(payload)

    @app.post("/api/setup/events/{event_id}/categories")
    async def setup_add_category(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(
                categories.add_poi_category, conn, event_id,
                body.get("name", ""), bool(body.get("staffed")),
                body.get("icon") or "pin", body.get("color"),
            )
            conn.commit()
        finally:
            conn.close()
        return JSONResponse(dict(row), status_code=201)

    @app.post("/api/setup/events/{event_id}/categories/{key}")
    async def setup_update_category(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(
                categories.update_poi_category, conn, event_id, key, body
            )
            conn.commit()
        finally:
            conn.close()
        return JSONResponse(dict(row))

    @app.post("/api/setup/events/{event_id}/categories/{key}/delete")
    async def setup_delete_category(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            in_use = _guard(categories.delete_poi_category, conn, event_id, key)
            conn.commit()
        finally:
            conn.close()
        if in_use:
            # Deleting the layer would leave its places drawn in no layer at
            # all - present in the database, invisible on the map, no error.
            raise HTTPException(
                status_code=409,
                detail=f"{in_use} place(s) still use this layer. "
                       "Move or delete them first.",
            )
        return JSONResponse({"deleted": key})

    @app.post("/api/setup/events/{event_id}/roles/{key}")
    async def setup_rename_role(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(
                categories.rename_roster_role, conn, event_id, key,
                body.get("name", ""),
            )
            conn.commit()
        finally:
            conn.close()
        return JSONResponse(dict(row))

    @app.post("/api/setup/events/{event_id}/roster")
    async def setup_save_roster(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            return JSONResponse(
                _guard(admin.save_roster_entry, conn, event_id, body)
            )
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/roster/delete")
    async def setup_delete_roster(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            admin.delete_roster_entry(conn, event_id, body.get("station_key", ""))
        finally:
            conn.close()
        return JSONResponse({"ok": True})

    # --- setup: access links ------------------------------------------------

    @app.get("/api/setup/events/{event_id}/links")
    async def setup_links(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            access.ensure_tokens(conn, event_id)
            event = conn.execute(
                "SELECT slug FROM event WHERE id = ?", (event_id,)
            ).fetchone()
            return JSONResponse({
                "slug": event["slug"],
                "links": admin.list_links(conn, event_id),
            })
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/links")
    async def setup_link_action(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        action = (body.get("action") or "").strip()
        try:
            if action == "revoke":
                access.revoke(conn, int(body.get("token_id")))
            elif action == "reissue":
                role = str(body.get("role", ""))
                if role not in access.ROLES:
                    raise HTTPException(status_code=400, detail=f"Unknown role {role!r}")
                # Revoke the old one in the same step: reissuing without
                # revoking would quietly leave the leaked link working.
                for row in access.tokens_for_event(conn, event_id):
                    if row["role"] == role and not row["revoked"]:
                        access.revoke(conn, row["id"])
                access.create_token(conn, event_id, role)
            else:
                raise HTTPException(status_code=400, detail="Unknown action.")
            links = admin.list_links(conn, event_id)
        finally:
            conn.close()
        return JSONResponse({"links": links})

    # --- setup: organizations -----------------------------------------------

    @app.get("/api/setup/organizations")
    async def setup_organizations(request: Request) -> JSONResponse:
        conn, user = require_user(request)
        try:
            organizations = users.list_organizations(conn)
            if not user.is_system_admin:
                organizations = [o for o in organizations
                                 if o["id"] == user.organization_id]
        finally:
            conn.close()
        return JSONResponse({"organizations": organizations})

    @app.post("/api/setup/organizations")
    async def setup_create_organization(request: Request) -> JSONResponse:
        # Only the host adds clubs: this is the tenancy boundary itself.
        conn, user = require_system_admin(request)
        body = await _json_body(request, conn)
        try:
            organization = _guard(
                users.create_organization, conn,
                body.get("slug", ""), body.get("name", ""), body.get("contact"),
            )
        finally:
            conn.close()
        return JSONResponse(organization, status_code=201)

    @app.post("/api/setup/organizations/{organization_id}")
    async def setup_update_organization(
        organization_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_system_admin(request)
        body = await _json_body(request, conn)
        try:
            organization = _guard(
                users.update_organization, conn, organization_id, body
            )
        finally:
            conn.close()
        return JSONResponse(organization)

    @app.post("/api/setup/organizations/{organization_id}/delete")
    async def setup_delete_organization(
        organization_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_system_admin(request)
        try:
            conn.execute("DELETE FROM organization WHERE id = ?", (organization_id,))
        finally:
            conn.close()
        return JSONResponse({"deleted": organization_id})

    # --- setup: users -------------------------------------------------------

    @app.get("/api/setup/users")
    async def setup_users(request: Request) -> JSONResponse:
        conn, user = require_user_manager(request)
        try:
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
        finally:
            conn.close()

    @app.post("/api/setup/users")
    async def setup_create_user(request: Request) -> JSONResponse:
        conn, actor = require_user_manager(request)
        body = await _json_body(request, conn)
        role = body.get("role", "")
        try:
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
            created = _guard(
                users.create_user, conn, body.get("username", ""),
                body.get("password", ""), role, body.get("display_name"),
                int(organization_id) if organization_id else None,
            )
            users.set_events(conn, created.id,
                             [int(i) for i in body.get("event_ids", [])])
        finally:
            conn.close()
        return JSONResponse(created.as_dict(), status_code=201)

    @app.post("/api/setup/users/{user_id}")
    async def setup_update_user(user_id: int, request: Request) -> JSONResponse:
        conn, actor = require_user_manager(request)
        body = await _json_body(request, conn)
        try:
            target = users.get_user(conn, user_id)
            if not users.may_manage_user(conn, actor, target):
                raise HTTPException(status_code=403, detail="Not your administrator.")
            if "password" in body:
                _guard(users.set_password, conn, user_id, body["password"])
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
                users.set_events(conn, user_id,
                                 [int(i) for i in body["event_ids"]])
            result = users.get_user(conn, user_id).as_dict()
        finally:
            conn.close()
        return JSONResponse(result)

    @app.post("/api/setup/users/{user_id}/delete")
    async def setup_delete_user(user_id: int, request: Request) -> JSONResponse:
        conn, actor = require_user_manager(request)
        try:
            target = users.get_user(conn, user_id)
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
        finally:
            conn.close()
        return JSONResponse({"deleted": user_id})

    # --- pages -------------------------------------------------------------

    @app.get("/")
    async def index() -> JSONResponse:
        # No public landing page. Access is by role URL only.
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/e/{event_slug}/{token}")
    async def map_page(event_slug: str, token: str) -> HTMLResponse:
        conn, _ = require_access(event_slug, token)
        conn.close()
        # The manifest URL carries the token, because the app has no
        # tokenless entry point - a static start_url would install a shortcut
        # to a 404.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        manifest = f"/api/{event_slug}/{token}/manifest.webmanifest"
        return _page(html.replace("__MANIFEST_URL__", manifest))

    @app.get("/api/{event_slug}/{token}/manifest.webmanifest")
    async def manifest(event_slug: str, token: str) -> JSONResponse:
        """Per-event, per-role manifest.

        `start_url` points back at this exact role link, so "Add to Home Screen"
        lands on the right event with the right permissions. That does mean the
        bearer token is saved onto the phone's home screen, which is consistent
        with the link model but worth knowing - see docs/RUNBOOK.md.
        """
        conn, granted = require_access(event_slug, token)
        event = conn.execute(
            "SELECT name FROM event WHERE id = ?", (granted.event_id,)
        ).fetchone()
        conn.close()

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

    # --- api ---------------------------------------------------------------

    @app.get("/api/{event_slug}/{token}/state")
    async def state(event_slug: str, token: str) -> JSONResponse:
        conn, granted = require_access(event_slug, token)
        try:
            payload = build_state(conn, granted.event_id)
        finally:
            conn.close()
        payload["role"] = granted.role
        payload["role_label"] = granted.role_label
        payload["role"] = granted.role
        payload["can_write"] = granted.can_write
        # Per capability, so the client shows exactly the controls this role can
        # actually use. A button the server would refuse is worse than no button.
        payload["capabilities"] = sorted(granted.capabilities)
        return JSONResponse(payload)

    # --- writes ------------------------------------------------------------
    #
    # Every mutation names the capability it needs and goes through one check,
    # so widening a role is a change to access.ROLE_CAPABILITIES rather than a
    # rewrite of each endpoint. It used to be a single yes/no; SAG needs to work
    # its pickup queue without being able to revoke a link or edit the roster.

    async def _json_body(request: Request, conn) -> dict:
        try:
            body = await request.json()
        except Exception:
            conn.close()
            raise HTTPException(status_code=400, detail="Expected a JSON body.")
        if not isinstance(body, dict):
            conn.close()
            raise HTTPException(status_code=400, detail="Expected a JSON object.")
        return body

    def require_capability(event_slug: str, token: str, capability: str):
        conn, granted = require_access(event_slug, token)
        if not granted.can(capability):
            conn.close()
            # 403 here, not 404: the token is valid and its holder knows the
            # event exists. Hiding the reason would just be confusing.
            detail = (
                f"{granted.role_label} is read-only."
                if not granted.can_write
                else f"{granted.role_label} cannot change that."
            )
            raise HTTPException(status_code=403, detail=detail)
        return conn, granted

    @app.post("/api/{event_slug}/{token}/station/{station_key}/status")
    async def set_station_status(
        event_slug: str, token: str, station_key: str, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_STATIONS)
        try:
            body = await request.json()
        except Exception:
            conn.close()
            raise HTTPException(status_code=400, detail="Expected a JSON body.")

        op_status = str(body.get("op_status", "")).strip().lower()
        # Free-text initials typed once per shift. A log annotation for handover,
        # never authentication - do not start trusting it as identity.
        changed_by = (body.get("changed_by") or "").strip()[:12] or None

        try:
            row = db.set_op_status(
                conn, granted.event_id, station_key, op_status, changed_by
            )
        except ValueError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))

        payload = {
            "type": "station_status",
            "station_key": row["station_key"],
            "op_status": row["op_status"],
            "op_status_at": row["op_status_at"],
            "op_status_by": row["op_status_by"],
            "op_status_label": db.op_status_label(row["category"], row["op_status"]),
        }
        conn.close()
        # Everyone watching sees it immediately, including the read-only roles.
        await app.state.hub.publish(granted.event_id, payload)
        return JSONResponse(payload)

    async def _publish_state_hint(event_id: int) -> None:
        """Ask every client to reload full state.

        Adopting or ignoring an SSID rewrites the roster, which is more than an
        incremental message can express. A resync is cheap and cannot leave a
        client half-updated.
        """
        await app.state.hub.publish(event_id, {"type": "resync"})

    async def _publish_incident(event_id: int, row, kind: str) -> None:
        conn = get_conn()
        try:
            index = progress.CourseIndex.for_event(conn, event_id)
        finally:
            conn.close()
        payload = incidents.Incident(row).as_dict()
        payload["course_position"] = _course_position(index, row["lat"], row["lon"])
        payload["type"] = "incident"
        payload["change"] = kind
        await app.state.hub.publish(event_id, payload)

    @app.post("/api/{event_slug}/{token}/incidents")
    async def create_incident(
        event_slug: str, token: str, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENTS)
        body = await _json_body(request, conn)
        try:
            row = incidents.create(
                conn, granted.event_id,
                lat=float(body.get("lat")), lon=float(body.get("lon")),
                bib=body.get("bib"), note=body.get("note"),
                poi_id=body.get("poi_id"), by=body.get("changed_by"),
                kind=(body.get("kind") or incidents.KIND_PICKUP),
            )
        except (incidents.IncidentError, TypeError, ValueError) as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        await _publish_incident(granted.event_id, row, "created")
        return JSONResponse(incidents.Incident(row).as_dict(), status_code=201)

    @app.post("/api/{event_slug}/{token}/incidents/{incident_id}/status")
    async def set_incident_status(
        event_slug: str, token: str, incident_id: int, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENTS)
        body = await _json_body(request, conn)
        try:
            row = incidents.set_status(
                conn, granted.event_id, incident_id,
                str(body.get("status", "")).strip().lower(),
                by=body.get("changed_by"),
            )
        except incidents.IncidentError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        await _publish_incident(granted.event_id, row, "status")
        return JSONResponse(incidents.Incident(row).as_dict())

    @app.post("/api/{event_slug}/{token}/incidents/{incident_id}")
    async def update_incident(
        event_slug: str, token: str, incident_id: int, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENTS)
        body = await _json_body(request, conn)
        fields = {k: v for k, v in body.items()
                  if k in {"bib", "note", "assigned_to", "lat", "lon"}}
        try:
            row = incidents.update(
                conn, granted.event_id, incident_id,
                by=body.get("changed_by"), **fields,
            )
        except (incidents.IncidentError, TypeError, ValueError) as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        await _publish_incident(granted.event_id, row, "edited")
        return JSONResponse(incidents.Incident(row).as_dict())

    @app.post("/api/{event_slug}/{token}/ssid/adopt")
    async def adopt_ssid(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Point a roster entry at the SSID its operator is actually using."""
        conn, granted = require_capability(event_slug, token, access.CAP_SSID)
        body = await _json_body(request, conn)
        try:
            row = db.change_station_key(
                conn, granted.event_id,
                str(body.get("from_station_key", "")),
                str(body.get("to_station_key", "")),
            )
        except ValueError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        payload = {"station_key": row["station_key"],
                   "display_label": row["display_label"]}
        conn.close()
        await _publish_state_hint(granted.event_id)
        return JSONResponse(payload)

    @app.post("/api/{event_slug}/{token}/ssid/ignore")
    async def ignore_ssid(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Dismiss an SSID: a digipeater, igate or home station."""
        conn, granted = require_capability(event_slug, token, access.CAP_SSID)
        body = await _json_body(request, conn)
        station_key = str(body.get("station_key", "")).strip()
        if not station_key:
            conn.close()
            raise HTTPException(status_code=400, detail="A station_key is required.")
        db.exclude_station(conn, granted.event_id, station_key,
                           body.get("reason") or "dismissed from the map")
        conn.close()
        await _publish_state_hint(granted.event_id)
        return JSONResponse({"ignored": station_key.upper()})

    @app.get("/api/{event_slug}/{token}/station-log")
    async def station_log(
        event_slug: str, token: str, station_key: str | None = None
    ) -> JSONResponse:
        """Operational status history, for shift handover and after-action.

        Readable by every role: the incoming operator needs it regardless of
        whether they can write.
        """
        conn, granted = require_access(event_slug, token)
        try:
            entries = [
                {key: row[key] for key in row.keys()}
                for row in db.op_status_log(conn, granted.event_id, station_key)
            ]
        finally:
            conn.close()
        return JSONResponse({"entries": entries})

    @app.get("/api/{event_slug}/{token}/incidents/{incident_id}/log")
    async def incident_log(
        event_slug: str, token: str, incident_id: int
    ) -> JSONResponse:
        # Readable by every role: the log is what a shift handover reads.
        conn, granted = require_access(event_slug, token)
        try:
            incidents.get(conn, granted.event_id, incident_id)
            entries = [
                {key: row[key] for key in row.keys()}
                for row in incidents.log_for(conn, incident_id)
            ]
        except incidents.IncidentError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            conn.close()
        return JSONResponse({"entries": entries})

    async def _publish_leaders(event_id: int) -> None:
        conn = get_conn()
        try:
            index = progress.CourseIndex.for_event(conn, event_id)
            payload = {
                "type": "leaders",
                "leaders": [e.as_dict() for e in leaders.for_event(conn, event_id, index)],
            }
        finally:
            conn.close()
        await app.state.hub.publish(event_id, payload)

    @app.post("/api/{event_slug}/{token}/leaders/sighting")
    async def record_leader(
        event_slug: str, token: str, request: Request
    ) -> JSONResponse:
        """Log that a division's leader passed an aid station.

        This only ever comes from an operator reporting on the net - there is no
        tracker on the front runner - so it is a report, not a measurement.
        """
        conn, granted = require_capability(event_slug, token, access.CAP_LEADERS)
        body = await _json_body(request, conn)
        try:
            leaders.record_sighting(
                conn, granted.event_id,
                course_id=int(body.get("course_id")),
                division=str(body.get("division", "")),
                poi_id=int(body.get("poi_id")),
                bib=body.get("bib"),
                by=body.get("changed_by"),
            )
        except (ValueError, TypeError) as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        await _publish_leaders(granted.event_id)
        return JSONResponse({"ok": True}, status_code=201)

    @app.post("/api/{event_slug}/{token}/leaders/undo")
    async def undo_leader(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Remove the most recent sighting. Mis-taps happen on race day."""
        conn, granted = require_capability(event_slug, token, access.CAP_LEADERS)
        body = await _json_body(request, conn)
        try:
            removed = leaders.undo_last_sighting(
                conn, granted.event_id,
                int(body.get("course_id")), str(body.get("division", "")),
            )
        except (ValueError, TypeError) as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        if removed:
            await _publish_leaders(granted.event_id)
        return JSONResponse({"removed": removed})

    @app.post("/api/{event_slug}/{token}/course/{course_id}/bib-color")
    async def set_bib_color(
        event_slug: str, token: str, course_id: int, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_COURSE)
        body = await _json_body(request, conn)
        try:
            row = leaders.set_bib_color(
                conn, granted.event_id, course_id,
                body.get("bib_color"), body.get("bib_color_name"),
            )
        except ValueError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        payload = {
            "course_id": row["id"],
            "bib_color": row["bib_color"],
            "bib_color_name": row["bib_color_name"],
        }
        conn.close()
        await _publish_leaders(granted.event_id)
        return JSONResponse(payload)

    # --- live feed ---------------------------------------------------------

    @app.websocket("/ws/{event_slug}/{token}")
    async def live(websocket: WebSocket, event_slug: str, token: str) -> None:
        conn = db.connect(settings.db_path)
        granted = access.resolve(conn, event_slug, token)
        conn.close()
        if granted is None:
            await websocket.close(code=4404)
            return

        await websocket.accept()
        subscription = app.state.hub.subscribe(granted.event_id)
        try:
            while True:
                message = await subscription.queue.get()
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - transport level
            log.debug("WebSocket closed: %s", exc)
        finally:
            app.state.hub.unsubscribe(subscription)

    # --- static ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # --- ingest lifecycle --------------------------------------------------

    async def _ingest_for(slug: str) -> None:
        conn = db.connect(settings.db_path)
        event = db.get_event(conn, slug)
        if event is None:
            conn.close()
            log.error("Cannot ingest unknown event %r", slug)
            return
        roster_by_key = {
            row["station_key"]: row for row in db.roster_for_event(conn, event["id"])
        }
        # Course geometry is loaded once for the life of the ingest task rather
        # than per packet. Courses are set up before the event and do not change
        # while it runs; restart the server if one is re-imported mid-event.
        index = progress.CourseIndex.for_event(conn, event["id"])
        conn.close()

        async def on_position(event_id: int, report) -> None:
            await app.state.hub.publish(
                event_id,
                hub_module.position_message(
                    report,
                    roster_by_key.get(report.station_key),
                    _course_position(index, report.lat, report.lon),
                ),
            )

        await run_ingest(settings, slug, on_position=on_position)

    return app
