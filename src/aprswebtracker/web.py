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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import access, db, hub as hub_module
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

    pois = [
        _row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM poi WHERE event_id = ? ORDER BY poi_type, name",
            (event_id,),
        ).fetchall()
    ]

    roster = [
        _row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label",
            (event_id,),
        ).fetchall()
    ]

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
        title="AprsWebTracker", docs_url=None, redoc_url=None, lifespan=lifespan
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
    async def map_page(event_slug: str, token: str) -> FileResponse:
        conn, _ = require_access(event_slug, token)
        conn.close()
        return FileResponse(STATIC_DIR / "index.html")

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
        roster_by_key = {
            row["station_key"]: row
            for row in db.roster_for_event(conn, event["id"])
        } if event else {}
        conn.close()
        if event is None:
            log.error("Cannot ingest unknown event %r", slug)
            return

        async def on_position(event_id: int, report) -> None:
            await app.state.hub.publish(
                event_id,
                hub_module.position_message(report, roster_by_key.get(report.station_key)),
            )

        await run_ingest(settings, slug, on_position=on_position)

    return app
