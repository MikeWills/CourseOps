"""The web server: state snapshot, live WebSocket feed, and the map page.

Access is by role token in the URL path. There is no public view — every route
below resolves a token or returns 404. 404 rather than 403 is deliberate: an
invalid token should not confirm that an event exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import importlib.metadata as _metadata
import logging
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, File, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles

from . import (access, admin, build, categories, db, guides, hub as hub_module, importer,
               incidents, labels as poi_labels, report, resources,
               kml, leaders, progress, symbols, users)
from .config import Settings
from . import ingest as ingest_module
from .ingest import run_ingest

log = logging.getLogger(__name__)

STATIC_DIR = resources.package_file("static")

# One source of truth for the version, and it is the package, not the install.
#
# This read `importlib.metadata` alone, which is what an editable install wrote
# into its dist-info the day it was created and never revisits - so a working
# copy six releases along reported the version it had when `pip install -e .`
# was first run, while `aprsis.py` announced the real one in its login string
# and `build.py` printed it beside the commit. Two version numbers for one
# process, disagreeing, with nothing to catch it: the release workflow checks a
# tag against the packaged version and cannot see this.
#
# `__init__.py` wins because it always ships. Metadata is precisely what is
# missing from the frozen Windows build, which is why the fallback exists at
# all - and a distribution version that disagrees with the package is a broken
# install rather than a second opinion worth publishing.
try:
    from . import __version__
except Exception:            # pragma: no cover - the package always ships this
    try:
        __version__ = _metadata.version("courseops")
    except Exception:        # running from a source tree with no install
        __version__ = "0.0.0+source"

SESSION_COOKIE = "courseops_session"
# The same cookie over HTTPS. The `__Host-` prefix is enforced by the browser:
# it will only store the cookie if it is Secure, has no Domain and its path is
# `/`, and no other host - not a sibling app under the same registrable
# domain - can set a cookie of that name for us. Over plain HTTP (local
# development, the Windows build on a LAN) the prefix would make the browser
# drop the cookie, so the plain name stays for that case.
SECURE_SESSION_COOKIE = "__Host-" + SESSION_COOKIE

# The most any JSON request may carry. The biggest real body is a reorder of
# a few hundred ids, well under a kilobyte; the cap is generous so a large
# roster cannot hit it and small enough that a flood of them costs nothing.
# The course file upload is the one exception and has its own cap in kml.py.
MAX_JSON_BYTES = 64 * 1024

# Where the one large upload arrives. Everything else is held to
# MAX_JSON_BYTES before a byte of it is read.
_IMPORT_PATH = re.compile(r"^/api/setup/events/\d+/import$")

# Methods that change something. Everything else on the setup API is a read,
# and stays one (see refuse_cross_site_setup_writes).
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _header_host(value: str) -> str | None:
    """The host[:port] named by an Origin or Referer header, or None if the
    header names nothing a page of ours could have sent."""
    value = (value or "").strip()
    if not value or value.lower() == "null":
        return None
    from urllib.parse import urlsplit
    try:
        return urlsplit(value).netloc.lower() or None
    except ValueError:
        return None


def request_is_same_origin(request: Request) -> bool:
    """Whether a state-changing request came from a page we served.

    Browsers name the page a request was made from in `Origin` (every
    cross-origin request, and every POST in current browsers) or `Referer`;
    a page on another host cannot forge either. The comparison is against the
    Host the request was addressed to, which behind Apache is the public name
    because the vhost sets ProxyPreserveHost.

    A request carrying neither header did not come from a browser page - a
    script, a test, the CLI - and passes: the cookie it would need is not in
    its hands unless it is ours. `Origin: null` is a sandboxed frame or a
    redirect chain, and is refused.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        return _header_host(origin) == request.headers.get("host", "").lower()
    referer = request.headers.get("referer")
    if referer:
        return _header_host(referer) == request.headers.get("host", "").lower()
    return True

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
    # `name` is the URL path, so the file is looked up under static/ by the
    # part after /static/ - a bare basename lost the leaflet/ directory and
    # stamped the vendored files "0" forever.
    try:
        relative = name.removeprefix("/static/")
        return str(int((STATIC_DIR / relative).stat().st_mtime))
    except OSError:
        return "0"


# Where the map tiles come from. Named in the Content-Security-Policy, so a
# change of tile provider (#3) is a change here too.
TILE_ORIGIN = "https://tile.openstreetmap.org"


def security_headers(host: str) -> dict[str, str]:
    """The headers every response carries, set by the app so the Windows build
    and a LAN install get them, not only a server behind the shipped Apache
    config.

    The policy is 'self' for everything, with two named exceptions: the tile
    server for images, and the page's own host for the WebSocket - spelled
    out as ws:/wss: because older WebKit does not read 'self' as covering
    them. Inline STYLE is allowed because Leaflet positions every marker with
    a style attribute; inline SCRIPT is not, and that is the point: the two
    clients build markup from server data all day, and with no inline script
    permitted an escaping slip becomes a blocked request instead of a stolen
    token.
    """
    sockets = f" ws://{host} wss://{host}" if host else ""
    csp = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        f"img-src 'self' data: blob: {TILE_ORIGIN}",
        f"connect-src 'self'{sockets}",
        "font-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ])
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        # NOT same-origin: the token is in the path and must never reach a
        # third party, but same-origin sends NO Referer to the tile server,
        # and OSM serves an "Access blocked" tile to traffic it cannot
        # attribute to a site. This sends the origin alone cross-site.
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "SAMEORIGIN",
    }


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

# How long a WebSocket may carry nothing before the server says something on
# its own. A phone cannot tell a quiet net from a dead socket - both are
# silence, and the badge reads "Live" for both - and a socket that dies
# without a close frame (a phone that slept, a NAT that forgot) never fires
# `close`. Uvicorn pings at the protocol level, which the browser answers
# without telling the page; this is the heartbeat the page can see. The
# client gives up on a socket after three of these have failed to arrive.
HEARTBEAT_SECONDS = 60

# How long a burst of setup saves is given to finish before the field is told
# to resync, and the most a resync may be held back while saves keep coming.
# Save-all posts one request per changed row; without this each row was a
# full snapshot fetch and a map rebuild on every phone.
RESYNC_DELAY_SECONDS = 0.3
RESYNC_MAX_WAIT_SECONDS = 1.5


# A link's label is a note to the officer handing links out - "Dana, phone" -
# so it is trimmed and capped and never validated further. Nothing reads it but
# a human deciding which row to revoke.
MAX_LINK_LABEL = 60


def _link_label(value: object) -> str | None:
    text = str(value or "").strip()[:MAX_LINK_LABEL]
    return text or None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


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


def _course_position(index: "progress.CourseIndex", lat: float, lon: float):
    located = index.locate(lat, lon)
    return located.as_dict() if located else None


def make_position_handler(hub, known_keys: set[str], index):
    """The ingest callback: fan a position out, and announce a new station.

    The SSID alerts ("Needs attention") are computed from stored positions
    and sent with the state snapshot, so a station the roster does not name
    used to reach every browser as a marker while the alert about it waited
    for someone to refresh. That defeats the alert: a wrong SSID is a silent
    failure, and the whole point is to put it in front of NCS unprompted.

    So the first packet from a station the roster does not know triggers one
    resync, which reloads the snapshot and with it the alerts. Once per
    station for the life of the feed - not per packet, which would have every
    browser reloading on every beacon from an igate.
    """
    announced: set[str] = set()

    async def on_position(event_id: int, report) -> None:
        await hub.publish(
            event_id,
            hub_module.position_message(
                report, _course_position(index, report.lat, report.lon)),
        )
        key = report.station_key
        if key not in known_keys and key not in announced:
            announced.add(key)
            await hub.publish(event_id, {"type": "resync"})

    return on_position


# The most stations held in memory per event. The area filter can deliver a
# whole town's worth on a busy band; beyond this the oldest is dropped, and
# it will be heard again if it is still there.
NEARBY_MAX = 200


def make_nearby_handler(hub, store: dict, index):
    """Stations heard near the course that the roster does not know.

    Held in memory only and sent only to a role that can act on them. This is
    the deal that makes an area filter acceptable: the public is SEEN by NCS,
    so a borrowed rig or a club tracker can be matched to the person using
    it, and nothing about anyone is written down until NCS says who they are.
    A restart empties the list, which costs one beacon interval.
    """
    async def on_nearby(event_id: int, report) -> None:
        entries = store.setdefault(event_id, {})
        previous = entries.get(report.station_key)
        located = index.locate(report.lat, report.lon)
        entry = {
            "station_key": report.station_key,
            "received_at": report.received_at,
            "lat": report.lat,
            "lon": report.lon,
            "symbol": symbols.describe(report.symbol_table, report.symbol_code),
            "looks_like_infrastructure": symbols.is_infrastructure(
                report.symbol_table, report.symbol_code),
            "packets": (previous["packets"] + 1) if previous else 1,
            "course_position": located.as_dict() if located else None,
        }
        entries[report.station_key] = entry
        if len(entries) > NEARBY_MAX:
            oldest = min(entries.values(), key=lambda e: e["received_at"])
            entries.pop(oldest["station_key"], None)
        await hub.publish(event_id, {"type": "nearby", **entry},
                          requires=access.CAP_SSID)

    return on_nearby


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
        # One or two characters for the pin itself. Derived unless the club
        # typed an override; the client never has to guess.
        entry["label_text"] = poi_labels.for_poi(row["name"], row["label"])
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
                # Per layer: a dozen aid stations are worth labelling, fifty
                # mile markers would bury the map in digits.
                "show_labels": bool(row["show_labels"]),
            }
            for row in categories.poi_categories(conn, event_id)
        ],
        "pois": pois,
        "roster": roster,
        "positions": positions,
        # ssid_alerts is NOT here: it is roster-adjacent and only a role
        # holding CAP_SSID can act on it, so `state()` adds it for those
        # roles alone - left out for the rest, like everything role-gated.
        "leaders": [entry.as_dict() for entry in
                    leaders.for_event(conn, event_id, index)],
        # Which leaders the event tracks is not sent as its own list: each
        # `leaders` entry carries its `division` and `division_label`, which
        # is the only form the panel reads (a row per race per leader).
        "incidents": incident_rows,
        "incident_statuses": [
            {"value": value, "label": incidents.STATUS_LABELS[value]}
            for value in incidents.STATUSES
        ],
        # No count of waiting pickups: the client derives it from the list
        # (`incidentDone` in app.js) and has to, because the list changes
        # under it on every socket message and a count sent once would be
        # stale by the second one.
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
        # A slug on the command line means "run this one now", so it is
        # recorded as the desired state rather than kept only in memory -
        # otherwise the switch in the UI would show "off" for a feed that is
        # plainly running.
        conn = db.connect(settings.db_path)
        try:
            db.init_schema(conn)
            for slug in ingest_events or []:
                db.set_ingest_enabled(conn, slug, True)
            wanted = db.events_wanting_ingest(conn)
        finally:
            conn.close()

        # One connection for the whole server, so at most one feed starts.
        # The switch turns the displaced event's flag off as it goes, so two
        # flagged events means a database from before it did - or a slug on
        # the command line beside a stale flag. The command line is explicit
        # and wins; otherwise the newest event, which is the likelier live
        # one against an old rehearsal. Starting them all in id order used
        # to start the first and cancel it for the second, silently.
        if wanted:
            named = [slug for slug in (ingest_events or []) if slug in wanted]
            chosen = named[-1] if named else wanted[-1]
            for slug in wanted:
                if slug != chosen:
                    await _displace_ingest(slug, by=chosen)
            await _start_ingest(chosen)
        try:
            yield
        finally:
            for slug in list(application.state.ingest_tasks):
                await _stop_ingest(slug)
            # A resync still waiting on a burst of saves has nobody left to
            # reach; drop it rather than leave a pending task at loop close.
            for task in list(resync_tasks):
                task.cancel()

    app = FastAPI(
        title="Course Ops", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.hub = hub_module.Hub()
    # slug -> task. A dict rather than a list because the feed is now started
    # and stopped while the server runs, and "is this one already going?" has
    # to be answerable without scanning.
    app.state.ingest_tasks = {}
    # Stations heard near the course that the roster does not know, per
    # event. Memory only, on purpose - see make_nearby_handler.
    app.state.nearby = {}
    # Why the last attempt stopped, if it did. A feed that fails to start is
    # the kind of failure nobody notices until the net is quiet for the wrong
    # reason, so it is kept and shown rather than only logged.
    app.state.ingest_errors = {}
    # Recent sign-in failures, by username and by address. Per application
    # rather than module-level so every test gets a clean one - the suite
    # signs in hundreds of times and must never throttle itself.
    app.state.login_limiter = users.LoginLimiter()

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
    #
    # Two things under an event change nothing a phone draws and are left
    # out: the tracking switch and the links. Both are worked on race
    # morning, when every phone rebuilding its map for nothing is the wrong
    # kind of activity.
    _SETUP_EVENT_PATH = re.compile(
        r"^/api/setup/events/(\d+)(?:/(?!tracking(?:/|$)|links(?:/|$))|$)")

    # Save-all posts one request per changed row, so twelve renames arrive
    # as twelve POSTs a few milliseconds apart - and each used to publish its
    # own resync, which is a full snapshot fetch and a map rebuild on every
    # phone, twelve times over. A burst is coalesced per event: the first
    # POST starts a short timer, each further one pushes it out a little,
    # and RESYNC_MAX_WAIT_SECONDS caps how long a long burst can hold the
    # field back. Nothing is lost by waiting: a resync is "fetch everything",
    # so the last one covers all that came before it.
    resync_due: dict[int, float] = {}       # event_id -> loop time to publish
    resync_tasks: set[asyncio.Task] = set()

    async def _publish_resync_when_quiet(event_id: int) -> None:
        loop = asyncio.get_running_loop()
        latest = loop.time() + RESYNC_MAX_WAIT_SECONDS
        while True:
            now = loop.time()
            due = min(resync_due.get(event_id, now), latest)
            if now >= due:
                break
            await asyncio.sleep(due - now)
        resync_due.pop(event_id, None)
        # A resync rather than a diff: setup edits rewrite whole sets -
        # layers, roster, courses - which no incremental message expresses,
        # and a resync cannot leave a client half-updated.
        await app.state.hub.publish(event_id, {"type": "resync"})

    def request_resync(event_id: int) -> None:
        loop = asyncio.get_running_loop()
        already = event_id in resync_due
        resync_due[event_id] = loop.time() + RESYNC_DELAY_SECONDS
        if already:
            return
        task = asyncio.create_task(_publish_resync_when_quiet(event_id))
        resync_tasks.add(task)
        task.add_done_callback(resync_tasks.discard)

    app.state.request_resync = request_resync

    @app.middleware("http")
    async def publish_setup_changes(request: Request, call_next):
        response = await call_next(request)
        if request.method != "POST" or response.status_code >= 400:
            return response
        match = _SETUP_EVENT_PATH.match(request.url.path)
        if match:
            request_resync(int(match.group(1)))
        return response

    # The setup API is cookie-authenticated, and SameSite=Lax is a same-SITE
    # rule, not a same-origin one: anything else hosted under the same
    # registrable domain - this VPS hosts more than one app - could POST to
    # the tracking switch, delete an event or revoke every link with the
    # officer's cookie attached, and any browser that does not enforce
    # SameSite fails open. Refused here, in one place, for every method that
    # writes: a new setup route gets it for free and none can forget it. The
    # field API is left alone - its credential is in the path, so a page that
    # can make the request already holds the token.
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in security_headers(request.headers.get("host", "")).items():
            response.headers.setdefault(name, value)
        return response

    @app.middleware("http")
    async def refuse_cross_site_setup_writes(request: Request, call_next):
        if (request.method in _WRITE_METHODS
                and request.url.path.startswith("/api/setup/")
                and not request_is_same_origin(request)):
            return JSONResponse(
                {"detail": "Cross-site request refused."}, status_code=403)
        return await call_next(request)

    # Nothing capped request bodies: the login route, which needs no
    # credential, would buffer a multi-hundred-megabyte POST in RAM before
    # looking at it, and the import read a whole upload into memory before
    # the parser's own limit applied. The Windows build has no proxy in
    # front of it at all. Refused here on Content-Length, before the body is
    # read; a body that lies about its length, or sends none, is stopped by
    # the readers below, which count as they go.
    def body_limit(path: str) -> int:
        if _IMPORT_PATH.match(path):
            # The parser's cap plus room for the multipart framing.
            return kml.MAX_KML_BYTES + 64 * 1024
        return MAX_JSON_BYTES

    @app.middleware("http")
    async def refuse_oversized_bodies(request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                return JSONResponse({"detail": "Bad Content-Length."},
                                    status_code=400)
            if length > body_limit(request.url.path):
                return JSONResponse({"detail": "Request body too large."},
                                    status_code=413)
        return await call_next(request)

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

    def session_token(request: Request) -> str:
        # Either name: a browser that reached us over HTTPS holds the
        # prefixed cookie, one on plain HTTP the bare one.
        return (request.cookies.get(SECURE_SESSION_COOKIE)
                or request.cookies.get(SESSION_COOKIE, ""))

    def current_user(request: Request, conn) -> users.User | None:
        return users.resolve_session(conn, session_token(request))

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
        # Existence first: may_access_event says yes to a system admin before
        # looking the event up, and a stale bookmark to a deleted event's
        # setup page was then a traceback from whichever route dereferenced
        # the missing row. For anyone else the answer is 403 either way, so
        # nothing is confirmed that was not already.
        if conn.execute("SELECT 1 FROM event WHERE id = ?",
                        (event_id,)).fetchone() is None:
            conn.close()
            raise HTTPException(status_code=404, detail="No such event.")
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
        wait = app.state.login_limiter.retry_after(*keys)
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

    @app.get("/robots.txt")
    async def robots() -> PlainTextResponse:
        """Keep every crawler out. Nothing here is meant to be found by
        search: the role pages are bearer links, and a link that gets
        indexed is a link handed to everyone. Pages also carry a noindex
        meta, because robots.txt only asks crawlers not to FETCH a page -
        a URL that reaches a search engine some other way (a shared link,
        a browser extension) can still be listed by address alone."""
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Is this instance actually working? Used by the deploy to decide
        whether to keep a new version or roll back.

        Checks the database rather than just answering, because "the process is
        up" and "the app works" are different claims and a deploy that only
        proves the first will happily leave a broken version running.

        Deliberately says nothing about the event: this is reachable without a
        token, so it reports liveness and a version and no more. An unauthorised
        caller learns that Course Ops is here, which they already knew from the
        page they are looking at.
        """
        try:
            conn = get_conn()
            try:
                conn.execute("SELECT 1 FROM event LIMIT 1").fetchone()
            finally:
                conn.close()
        except Exception:
            log.exception("Health check could not reach the database")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "version": __version__})

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

    # The after-event page for the race lead: pickups counted, notes listed,
    # no names. Behind the admin login - it is the club that prints or
    # screenshots this and hands it over, not the organizer following a link.
    @app.get("/setup/events/{event_id}/report")
    async def setup_report(event_id: int, request: Request) -> HTMLResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            # Off the loop: this walks every incident and sighting of the
            # event, and the live map must not pause while the officer reads.
            data = await asyncio.to_thread(report.build, conn, event_id)
        finally:
            conn.close()
        return HTMLResponse(report.render(data),
                            headers={"Cache-Control": "no-cache, must-revalidate"})

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
            # Which build is running. Here rather than on /healthz, which is
            # unauthenticated and deliberately says nothing beyond liveness and
            # a version: the exact commit tells anyone who asks precisely which
            # code is deployed, and that is a free gift to somebody looking for
            # a version with a known problem.
            "version": __version__,
            "build": build.build_id() if user else "",
        })

    @app.post("/api/setup/first-user")
    async def create_first_user(request: Request) -> JSONResponse:
        """Create the first system administrator, once.

        Open only while no users exist - after that it is closed permanently,
        so it cannot be used to add an admin to a running system.

        Does NOT start a session: the account is created and the person then
        signs in with it.
        """
        body = await _json_body(request)
        username = str(body.get("username", "") or "")
        keys = _refuse_if_throttled(request, username)

        def create() -> users.User:
            # Its own connection, opened in the worker thread: a sqlite
            # connection refuses to be used from any thread but the one
            # that opened it, and the hash has to run off the loop.
            conn = get_conn()
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
            # A rejected password is a failure worth counting: this route is
            # open to the whole internet until the first account exists.
            app.state.login_limiter.failed(*keys)
            raise HTTPException(status_code=400, detail=str(exc))
        # Deliberately no session: they sign in with the account straight away,
        # which proves the password works while they still remember typing it.
        # This is a credential they may not use again until the next event.
        return JSONResponse({"user": user.as_dict(), "created": True},
                            status_code=201)

    @app.post("/api/setup/login")
    async def login(request: Request) -> JSONResponse:
        """Sign an administrator in.

        The hash runs in a worker thread, on a connection opened there. It
        ran on the event loop once: a third of a second per attempt during
        which the WebSocket fan-out, the snapshots and the incident posts all
        waited - so two wrong passwords a second from anyone at all, with no
        credential, froze the map for every volunteer, and on the phones it
        looked exactly like a bad signal.
        """
        body = await _json_body(request)
        username = str(body.get("username", "") or "")
        keys = _refuse_if_throttled(request, username)

        def sign_in() -> tuple[users.User, str]:
            conn = get_conn()
            try:
                user = users.authenticate(conn, username, body.get("password", ""))
                return user, users.start_session(conn, user.id)
            finally:
                conn.close()

        try:
            user, token = await asyncio.to_thread(sign_in)
        except users.AuthError as exc:
            app.state.login_limiter.failed(*keys)
            raise HTTPException(status_code=401, detail=str(exc))
        app.state.login_limiter.succeeded(*keys)
        response = JSONResponse({"user": user.as_dict()})
        _set_session_cookie(response, token, request_is_secure(request))
        return response

    @app.post("/api/setup/logout")
    async def logout(request: Request) -> JSONResponse:
        conn = get_conn()
        try:
            users.end_session(conn, session_token(request))
        finally:
            conn.close()
        response = JSONResponse({"ok": True})
        _clear_session_cookie(response)
        return response

    @app.post("/api/setup/password")
    async def change_own_password(request: Request) -> JSONResponse:
        conn, user = require_user(request)
        conn.close()
        body = await _json_body(request)
        keys = _refuse_if_throttled(request, user.username)

        def change() -> None:
            conn = get_conn()
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
            app.state.login_limiter.failed(*keys)
            raise HTTPException(status_code=400, detail=str(exc))
        app.state.login_limiter.succeeded(*keys)
        response = JSONResponse({"ok": True})
        _clear_session_cookie(response)   # sessions were cleared
        return response

    # --- setup: events -----------------------------------------------------

    def _guard(fn, *args):
        """Turn a domain error into a 400 with its message.

        The caller's try/finally closes the connection. TypeError is here
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
            event = _guard(_create_event, conn, body, organization_id)
        finally:
            conn.close()
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
            row = conn.execute(
                "SELECT slug FROM event WHERE id = ?", (event_id,)).fetchone()
            admin.delete_event(conn, event_id)
        finally:
            conn.close()
        # The feed must not outlive the event - see _forget_ingest.
        if row is not None:
            await app.state.forget_ingest(row["slug"], event_id)
        return JSONResponse({"deleted": event_id})

    # --- setup: course import ----------------------------------------------

    @app.post("/api/setup/events/{event_id}/import")
    async def setup_import(
        event_id: int, request: Request, file: UploadFile = File(...)
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
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
        try:
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
        finally:
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

    # Literal before parameterised, or "reorder" parses as a course id.
    @app.post("/api/setup/events/{event_id}/courses/reorder")
    async def setup_reorder_courses(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            count = _guard(
                admin.reorder_courses, conn, event_id, body.get("course_ids") or [])
        finally:
            conn.close()
        return JSONResponse({"ordered": count})

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
            blocked = admin.delete_course(conn, event_id, course_id)
        finally:
            conn.close()
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
    @app.post("/api/setup/events/{event_id}/pois")
    async def setup_add_poi(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(admin.create_poi, conn, event_id, body)
        finally:
            conn.close()
        return JSONResponse(row, status_code=201)

    @app.post("/api/setup/events/{event_id}/pois/reorder")
    async def setup_reorder_pois(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            count = _guard(
                admin.reorder_pois, conn, event_id, body.get("poi_ids") or [])
        finally:
            conn.close()
        return JSONResponse({"ordered": count})

    @app.post("/api/setup/events/{event_id}/pois/move")
    async def setup_move_pois(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            moved = _guard(
                admin.move_pois, conn, event_id,
                body.get("poi_ids") or [], (body.get("poi_type") or "").strip(),
            )
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
            blocked = admin.delete_poi(conn, event_id, poi_id)
        finally:
            conn.close()
        if blocked:
            # Sightings would cascade away and the posted operator would fall
            # off the map, neither with anything on screen to say why.
            raise HTTPException(
                status_code=409,
                detail=f"{blocked} at this place. Clear the sightings and "
                       "move the stations first.")
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

    def _tracking_state(conn, event_id: int) -> dict:
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
        }

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

    @app.get("/api/setup/events/{event_id}/tracking")
    async def setup_tracking(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            return JSONResponse(_guard(_tracking_state, conn, event_id))
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/tracking")
    async def setup_set_tracking(event_id: int, request: Request) -> JSONResponse:
        """Turn this event's APRS-IS feed on or off.

        Off outside race day is the intended state, not an oversight: the
        filter matches each operator's callsign wherever they are, so a feed
        left running logs where volunteers live and work for as long as it is
        up. See docs/PLAN.md.
        """
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        wanted = bool(body.get("enabled"))
        try:
            state = _guard(_tracking_state, conn, event_id)
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
        finally:
            conn.close()

        if not wanted:
            await app.state.stop_ingest(slug)
        else:
            # Start first, persist second. The flag is what the next boot
            # acts on, so it must describe a feed that actually started: a
            # flag written before the attempt turned one refused press into
            # a service that restarted into the same failure under systemd.
            started = await app.state.start_ingest(slug)
            if not started:
                raise HTTPException(
                    status_code=400,
                    detail=app.state.ingest_errors.get(slug)
                    or "The feed stopped before it connected.")
            conn = get_conn()
            try:
                db.set_ingest_enabled(conn, slug, True)
            finally:
                conn.close()

        conn = get_conn()
        try:
            return JSONResponse(_tracking_state(conn, event_id))
        finally:
            conn.close()

    @app.get("/api/setup/events/{event_id}/categories")
    async def setup_categories(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
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
        finally:
            conn.close()
        return JSONResponse(dict(row), status_code=201)

    # Literal before parameterised, or "reorder" is taken as a layer key.
    @app.post("/api/setup/events/{event_id}/categories/reorder")
    async def setup_reorder_categories(
        event_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            count = _guard(
                categories.reorder_poi_categories, conn, event_id,
                body.get("keys") or [])
        finally:
            conn.close()
        return JSONResponse({"ordered": count})

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

    @app.post("/api/setup/events/{event_id}/roles")
    async def setup_add_role(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(categories.add_roster_role, conn, event_id,
                         body.get("name") or "")
        finally:
            conn.close()
        return JSONResponse(dict(row), status_code=201)

    # Literal before parameterised: "/roles/{key}" would swallow this.
    @app.post("/api/setup/events/{event_id}/roles/{key}/delete")
    async def setup_delete_role(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            in_use = _guard(categories.delete_roster_role, conn, event_id, key)
        finally:
            conn.close()
        if in_use:
            # 409 like the other in-use refusals: the request was well
            # formed, it is the data that is in the way.
            raise HTTPException(
                status_code=409,
                detail=f"{in_use} roster entr{'y' if in_use == 1 else 'ies'} "
                       "still use this role. Move them first.")
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
        finally:
            conn.close()
        return JSONResponse(dict(row))

    # The leaders this event tracks - "First male", "First wheelchair". Called
    # leaders on screen and in these routes; the key stored on a sighting is
    # still `division`, which is internal and in databases that already exist.
    @app.post("/api/setup/events/{event_id}/leaders")
    async def setup_add_leader(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(categories.add_lead_division, conn, event_id,
                         body.get("name") or "")
        finally:
            conn.close()
        return JSONResponse(dict(row), status_code=201)

    # Literal before parameterised, or "reorder" parses as a leader key and the
    # drag handle silently does nothing.
    @app.post("/api/setup/events/{event_id}/leaders/reorder")
    async def setup_reorder_leaders(
        event_id: int, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            count = _guard(categories.reorder_lead_divisions, conn, event_id,
                           body.get("keys") or [])
        finally:
            conn.close()
        return JSONResponse({"ordered": count})

    @app.post("/api/setup/events/{event_id}/leaders/{key}/delete")
    async def setup_delete_leader(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
            in_use = _guard(categories.delete_lead_division, conn, event_id, key)
        finally:
            conn.close()
        if in_use:
            # The sightings would stay in the database and vanish from the
            # panel, with nothing on screen to say where they went.
            raise HTTPException(
                status_code=409,
                detail=f"{in_use} sighting{'' if in_use == 1 else 's'} "
                       "recorded against this leader. Clear them first.")
        return JSONResponse({"deleted": key})

    @app.post("/api/setup/events/{event_id}/leaders/{key}")
    async def setup_rename_leader(
        event_id: int, key: str, request: Request
    ) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        try:
            row = _guard(categories.rename_lead_division, conn, event_id, key,
                         body.get("name", ""))
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
            _guard(admin.delete_roster_entry, conn, event_id,
                   body.get("station_key", ""))
        finally:
            conn.close()
        return JSONResponse({"ok": True})

    # --- setup: access links ------------------------------------------------

    @app.get("/api/setup/events/{event_id}/links")
    async def setup_links(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        try:
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
        finally:
            conn.close()

    @app.post("/api/setup/events/{event_id}/links")
    async def setup_link_action(event_id: int, request: Request) -> JSONResponse:
        conn, user = require_event_admin(request, event_id)
        body = await _json_body(request, conn)
        action = (body.get("action") or "").strip()

        def token_id() -> int:
            try:
                return int(body.get("token_id"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Which link?")

        try:
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
            # Cascades through the organization's events, so their feeds go
            # the same way an event's own delete takes its feed with it.
            gone = conn.execute(
                "SELECT id, slug FROM event WHERE organization_id = ?",
                (organization_id,)).fetchall()
            conn.execute("DELETE FROM organization WHERE id = ?", (organization_id,))
        finally:
            conn.close()
        for event in gone:
            await app.state.forget_ingest(event["slug"], event["id"])
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
            created = _guard(_create_user, conn, body, role, organization_id)
        finally:
            conn.close()
        return JSONResponse(created.as_dict(), status_code=201)

    def _create_user(conn, body: dict, role, organization_id) -> users.User:
        # Everything checked before the INSERT: the connection is autocommit,
        # so validating event_ids after create_user left a half-made account
        # behind the error.
        org = _int(organization_id, "organization") if organization_id else None
        event_ids = _event_ids(conn, body.get("event_ids", []), org)
        with db.transaction(conn):
            created = users.create_user(
                conn, body.get("username", ""), body.get("password", ""), role,
                body.get("display_name"), org,
            )
            users.set_events(conn, created.id, event_ids)
        return created

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

    @app.post("/api/setup/users/{user_id}")
    async def setup_update_user(user_id: int, request: Request) -> JSONResponse:
        conn, actor = require_user_manager(request)
        body = await _json_body(request, conn)
        try:
            target = _get_user(conn, user_id)
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
                users.set_events(conn, user_id, _guard(
                    _event_ids, conn, body["event_ids"], target.organization_id))
            result = users.get_user(conn, user_id).as_dict()
        finally:
            conn.close()
        return JSONResponse(result)

    def _get_user(conn, user_id: int) -> users.User:
        # Two admins editing the same list - one deletes, the other saves -
        # is a 404 with a message, not a blank error.
        try:
            return users.get_user(conn, user_id)
        except users.AuthError:
            raise HTTPException(status_code=404, detail="No such administrator.")

    @app.post("/api/setup/users/{user_id}/delete")
    async def setup_delete_user(user_id: int, request: Request) -> JSONResponse:
        conn, actor = require_user_manager(request)
        try:
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
        finally:
            conn.close()
        return JSONResponse({"deleted": user_id})

    # --- pages -------------------------------------------------------------

    @app.get("/")
    async def index() -> RedirectResponse:
        # No public landing page: the field roles arrive by link and the
        # only thing at the domain itself is the club officer's sign-in. It
        # confirms Course Ops is here, which a 404 with our favicon on it
        # already did; what it must never do is name an event.
        return RedirectResponse("/setup", status_code=302)

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
            # Off the loop. The snapshot is the one heavy read in the live
            # app - 90 ms on the demo event, seconds on the real course - and
            # while it was being built on the loop nothing else moved: no
            # WebSocket send, no ingest, no other phone. A setup save resyncs
            # every phone at once, so twelve phones were twelve builds in a
            # row with positions frozen for the sum of them.
            payload = await asyncio.to_thread(build_state, conn, granted.event_id)
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
                payload["ssid_alerts"] = _ssid_alerts(conn, granted.event_id)
                payload["nearby"] = _nearby_for(conn, granted.event_id)
                # What has been ignored, so a mis-tap on Ignore can be
                # undone. An ignored station is silent in every other list,
                # which is the point of ignoring it and also what makes the
                # mistake invisible.
                payload["ignored"] = [
                    _row_to_dict(row) for row in db.exclusions(conn, granted.event_id)
                ]
        finally:
            conn.close()
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

    def _nearby_for(conn: sqlite3.Connection, event_id: int) -> list[dict[str, Any]]:
        known = set(db.all_station_keys(conn, event_id))
        known |= db.bound_station_keys(conn, event_id)
        known |= db.excluded_station_keys(conn, event_id)
        entries = app.state.nearby.get(event_id, {})
        for key in [k for k in entries if k in known]:
            entries.pop(key, None)         # assigned or dismissed since heard
        return sorted(
            entries.values(),
            key=lambda e: (e["course_position"] is None,
                           (e["course_position"] or {}).get("offset_m", 0)),
        )

    # --- writes ------------------------------------------------------------
    #
    # Every mutation names the capability it needs and goes through one check,
    # so widening a role is a change to access.ROLE_CAPABILITIES rather than a
    # rewrite of each endpoint. It used to be a single yes/no; SAG needs to work
    # its pickup queue without being able to revoke a link or edit the roster.

    async def _json_body(request: Request, conn=None) -> dict:
        # Read in chunks and counted, so a chunked body with no
        # Content-Length - which the middleware cannot size - is still cut
        # off at the cap rather than buffered whole.
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_JSON_BYTES:
                    raise HTTPException(status_code=413,
                                        detail="Request body too large.")
                chunks.append(chunk)
            body = json.loads(b"".join(chunks))
        except HTTPException:
            if conn is not None:
                conn.close()
            raise
        except Exception:
            if conn is not None:
                conn.close()
            raise HTTPException(status_code=400, detail="Expected a JSON body.")
        if not isinstance(body, dict):
            if conn is not None:
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
        body = await _json_body(request, conn)

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
            conn.close()
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
        # Same audience as the snapshot: a role that never receives the list
        # must not be handed its entries one at a time either.
        await app.state.hub.publish(event_id, payload,
                                    requires=access.CAP_INCIDENT_REPORT)

    # Reporting one is not the same permission as working the queue. Every role
    # is somewhere an incident can happen, so any of them may open one and
    # describe it; only NCS and SAG may move it along or take it off the board.
    @app.post("/api/{event_slug}/{token}/incidents")
    async def create_incident(
        event_slug: str, token: str, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENT_REPORT)
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

    @app.post("/api/{event_slug}/{token}/incidents/{incident_id}/delete")
    async def delete_incident(
        event_slug: str, token: str, incident_id: int, request: Request
    ) -> JSONResponse:
        """Remove a pickup or a course note that should never have existed."""
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENTS)
        try:
            row = incidents.delete(conn, granted.event_id, incident_id)
        except incidents.IncidentError as exc:
            conn.close()
            raise HTTPException(status_code=404, detail=str(exc))
        conn.close()
        # Every other browser has this in its list and on its map, and
        # nothing else will ever mention it again.
        await _publish_incident(granted.event_id, row, "deleted")
        return JSONResponse({"deleted": incident_id})

    @app.post("/api/{event_slug}/{token}/incidents/{incident_id}")
    async def update_incident(
        event_slug: str, token: str, incident_id: int, request: Request
    ) -> JSONResponse:
        conn, granted = require_capability(event_slug, token, access.CAP_INCIDENT_REPORT)
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
        app.state.nearby.get(granted.event_id, {}).pop(
            str(body.get("to_station_key", "")).strip().upper(), None)
        await _publish_state_hint(granted.event_id)
        return JSONResponse(payload)

    @app.post("/api/{event_slug}/{token}/ssid/unbind")
    async def unbind_ssid(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Undo a match: the roster entry goes back to waiting for a station."""
        conn, granted = require_capability(event_slug, token, access.CAP_SSID)
        body = await _json_body(request, conn)
        station_key = str(body.get("station_key", "")).strip().upper()
        row = conn.execute(
            "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
            (granted.event_id, station_key),
        ).fetchone()
        if row is None:
            conn.close()
            raise HTTPException(status_code=404, detail=f"{station_key} is not on the roster.")
        db.unbind_station(conn, granted.event_id, station_key)
        conn.close()
        await _publish_state_hint(granted.event_id)
        return JSONResponse({"station_key": row["station_key"],
                             "display_label": row["display_label"],
                             "was": row["bound_key"]})

    @app.post("/api/{event_slug}/{token}/ssid/ignore")
    async def ignore_ssid(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Dismiss an SSID: a digipeater, igate or home station."""
        conn, granted = require_capability(event_slug, token, access.CAP_SSID)
        body = await _json_body(request, conn)
        station_key = str(body.get("station_key", "")).strip()
        if not station_key:
            conn.close()
            raise HTTPException(status_code=400, detail="A station_key is required.")
        try:
            db.exclude_station(conn, granted.event_id, station_key,
                               body.get("reason") or "dismissed from the map")
        except ValueError as exc:
            conn.close()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.close()
        app.state.nearby.get(granted.event_id, {}).pop(station_key.upper(), None)
        await _publish_state_hint(granted.event_id)
        return JSONResponse({"ignored": station_key.upper()})

    @app.post("/api/{event_slug}/{token}/ssid/unignore")
    async def unignore_ssid(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Undo an Ignore. The station comes back the next time it is heard."""
        conn, granted = require_capability(event_slug, token, access.CAP_SSID)
        body = await _json_body(request, conn)
        station_key = str(body.get("station_key", "")).strip()
        if not station_key:
            conn.close()
            raise HTTPException(status_code=400, detail="A station_key is required.")
        removed = db.unexclude_station(conn, granted.event_id, station_key)
        conn.close()
        if not removed:
            raise HTTPException(status_code=404, detail=f"{station_key.upper()} was not ignored.")
        await _publish_state_hint(granted.event_id)
        return JSONResponse({"unignored": station_key.upper()})

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
        # Readable by every role that sees the queue: the log is what a
        # shift handover reads.
        conn, granted = require_capability(
            event_slug, token, access.CAP_INCIDENT_REPORT)
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
                division=body.get("division"),
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

    @app.post("/api/{event_slug}/{token}/leaders/reset")
    async def reset_leader(event_slug: str, token: str, request: Request) -> JSONResponse:
        """Clear every sighting for one race and division.

        Undo walks back one report at a time, which is no use to a club that
        rehearsed the panel the week before and wants a clean start.
        """
        conn, granted = require_capability(event_slug, token, access.CAP_LEADERS)
        body = await _json_body(request, conn)
        try:
            removed = leaders.clear_sightings(
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

    # --- the guides --------------------------------------------------------

    # Unauthenticated on purpose: these are the volunteer guides, public in
    # the repository already, and the `?` on a role page has to open without
    # asking anyone for anything. They name no event and hold no token. What
    # they must not do is leak a path: `guides.load` refuses anything that is
    # not a bare page name before touching the filesystem.
    @app.get("/help")
    @app.get("/help/")
    async def help_index() -> HTMLResponse:
        return _guide(guides.INDEX)

    @app.get("/help/{page}")
    async def help_page(page: str) -> HTMLResponse:
        return _guide(page)

    def _guide(name: str) -> HTMLResponse:
        page = guides.load(name)
        if page is None:
            raise HTTPException(status_code=404, detail="No such guide")
        html = guides.page_html(page, guides.page_names())
        return _page(html)

    # --- static ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # The screenshots, beside the pages they belong to. Declared after the
    # /help/{page} routes so a page named "images" could never shadow them -
    # a mount is matched after the routes above it.
    app.mount("/help/images", StaticFiles(directory=guides.GUIDES_DIR / "images"),
              name="guide-images")

    # --- ingest lifecycle --------------------------------------------------

    async def _ingest_for(slug: str) -> None:
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

        on_position = make_position_handler(app.state.hub, known_keys, index)
        on_nearby = make_nearby_handler(app.state.hub, app.state.nearby, index)
        await run_ingest(settings, slug, on_position=on_position,
                         on_nearby=on_nearby)

    async def _supervise_ingest(slug: str) -> None:
        """Run one feed, and remember why it stopped."""
        try:
            await _ingest_for(slug)
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

    async def _start_ingest(slug: str) -> bool:
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
                await _displace_ingest(running, by=slug)
        app.state.ingest_errors.pop(slug, None)
        app.state.ingest_tasks[slug] = asyncio.create_task(
            _supervise_ingest(slug), name=f"ingest:{slug}")
        await asyncio.sleep(0)
        return slug in app.state.ingest_tasks

    async def _stop_ingest(slug: str) -> None:
        task = app.state.ingest_tasks.pop(slug, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _event_name(slug: str) -> str:
        conn = db.connect(settings.db_path)
        try:
            event = db.get_event(conn, slug)
            return event["name"] if event else slug
        finally:
            conn.close()

    async def _displace_ingest(slug: str, by: str) -> None:
        """Turn one event's feed off because another's is going on.

        The persisted switch is what the next boot acts on, so the displaced
        event's flag has to come off with the feed: left on, the boot found
        two flagged, started the first and cancelled it for the second, and
        the displaced tab read "on - but not connected" with nothing to say
        why. The reason goes where the tab already looks for one.
        """
        await _stop_ingest(slug)
        conn = db.connect(settings.db_path)
        try:
            db.set_ingest_enabled(conn, slug, False)
        finally:
            conn.close()
        app.state.ingest_errors[slug] = (
            f"Tracking was turned on for {_event_name(by)}, and there is "
            "one APRS-IS connection for the whole server.")

    async def _forget_ingest(slug: str, event_id: int) -> None:
        """The event is gone; nothing about its feed may outlive it.

        Otherwise the connection keeps a wildcard filter on the deleted
        event's volunteers until the next restart, and re-creating the slug
        finds a feed "already running" that is bound to the dead event id.
        The nearby list is keyed by event id, the rest by slug.
        """
        await _stop_ingest(slug)
        app.state.ingest_errors.pop(slug, None)
        app.state.nearby.pop(event_id, None)

    app.state.start_ingest = _start_ingest
    app.state.stop_ingest = _stop_ingest
    app.state.forget_ingest = _forget_ingest

    return app
