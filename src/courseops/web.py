"""The web server, as bootstrap: build the application and nothing else.

`create_app` wires the pieces together - the lifespan that starts the one
APRS-IS feed, the middleware (security headers, the cross-site refusal, the
body caps, the resync that follows a setup change), the three routers and
the static mounts. The routes themselves live beside the people they serve:

    pages.py       what a browser navigates to: sign-in, the map, the guides
    setup_api.py   /api/setup/..., cookie-authenticated, for the setup app
    field_api.py   /api/{slug}/{token}/... and the WebSocket, by role link
    deps.py        the dependencies that resolve a link or a session and
                   hand a route its connection
    snapshot.py    the state a client draws from, and the feed callbacks
    feed.py        starting and stopping that feed

Access is by role token in the URL path. There is no public view - every
field route resolves a token or returns 404. 404 rather than 403 is
deliberate: an invalid token should not confirm that an event exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.metadata as _metadata
import logging
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (db, feed, field_api, guides, hub as hub_module, kml, pages,
               setup_api, users)
from .config import Settings
from .deps import MAX_JSON_BYTES
from .pages import STATIC_DIR

log = logging.getLogger(__name__)


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


# How long a burst of setup saves is given to finish before the field is told
# to resync, and the most a resync may be held back while saves keep coming.
# Save-all posts one request per changed row; without this each row was a
# full snapshot fetch and a map rebuild on every phone.
RESYNC_DELAY_SECONDS = 0.3
RESYNC_MAX_WAIT_SECONDS = 1.5


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
                    await feed.displace_ingest(application, slug, by=chosen)
            await feed.start_ingest(application, chosen)
        try:
            yield
        finally:
            for slug in list(application.state.ingest_tasks):
                await feed.stop_ingest(application, slug)
            # A resync still waiting on a burst of saves has nobody left to
            # reach; drop it rather than leave a pending task at loop close.
            for task in list(resync_tasks):
                task.cancel()

    # No /docs, no /redoc and no /openapi.json: the schema lists every route
    # to anyone who asks, which is the same reason /healthz stays minimal.
    app = FastAPI(
        title="Course Ops", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
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
    # What the first-user form has to be shown before it creates the system
    # administrator. Until that account exists the form is open to whoever
    # reaches /setup first - on a VPS that is the whole internet from the
    # moment TLS is up until the officer gets there, and a deploy that
    # recreated the database (a restore gone wrong, a wrong DB_PATH) would
    # reopen it silently. The code is printed where the server started,
    # so holding it means being at the console. New on every start: a code
    # that survived a restart would be a second password for the box.
    app.state.setup_code = secrets.token_hex(4).upper()

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

    # The routes read the version from here rather than importing it: this
    # module is the one place the fallback above is decided.
    app.state.version = __version__

    # Order matters only where paths could collide, and none of these do; the
    # routes that DO share a prefix live in one router, declared literal
    # before parameterised. The mounts come last: a mount is matched after
    # every route above it, so a guide page named "images" could never
    # shadow the screenshots.
    app.include_router(pages.router)
    app.include_router(setup_api.router)
    app.include_router(field_api.router)

    # --- static ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # The screenshots, beside the pages they belong to. Declared after the
    # /help/{page} routes so a page named "images" could never shadow them -
    # a mount is matched after the routes above it.
    app.mount("/help/images", StaticFiles(directory=guides.GUIDES_DIR / "images"),
              name="guide-images")

    # Bound to this app so a route or a test can say `start_ingest(slug)`
    # without carrying the application around; the implementation is feed.py.
    app.state.start_ingest = functools.partial(feed.start_ingest, app)
    app.state.stop_ingest = functools.partial(feed.stop_ingest, app)
    app.state.forget_ingest = functools.partial(feed.forget_ingest, app)

    return app
