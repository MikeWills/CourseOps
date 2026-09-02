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
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import access, db, hub as hub_module, progress
from .config import Settings
from .ingest import run_ingest

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")

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
        # An operator posted at an aid station inherits that station's place on
        # the course, so the roster can be read in course order too.
        if row["poi_id"] is not None:
            poi = next((p for p in pois if p["id"] == row["poi_id"]), None)
            if poi is not None:
                entry["course_position"] = poi["course_position"]
                entry["poi_name"] = poi["name"]
        roster.append(entry)

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
    ]

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
        "pois": pois,
        "roster": roster,
        "positions": positions,
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
        return HTMLResponse(html.replace("__MANIFEST_URL__", manifest))

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
        payload["can_write"] = granted.can_write
        return JSONResponse(payload)

    # --- writes ------------------------------------------------------------
    #
    # Every mutation goes through require_write() regardless of role, so
    # granting a field role write access later is a change to WRITE_ROLES
    # rather than a rewrite of each endpoint.

    def require_write(event_slug: str, token: str):
        conn, granted = require_access(event_slug, token)
        if not granted.can_write:
            conn.close()
            # 403 here, not 404: the token is valid and its holder knows the
            # event exists. Hiding the reason would just be confusing.
            raise HTTPException(
                status_code=403,
                detail=f"{granted.role_label} is read-only.",
            )
        return conn, granted

    @app.post("/api/{event_slug}/{token}/station/{station_key}/status")
    async def set_station_status(
        event_slug: str, token: str, station_key: str, request: Request
    ) -> JSONResponse:
        conn, granted = require_write(event_slug, token)
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
