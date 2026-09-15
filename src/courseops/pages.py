"""What a browser navigates to, as opposed to what the two clients fetch.

The sign-in page, the map page a role link opens, the after-event report,
the guides, and the two things a machine asks for (/healthz, robots.txt).
Kept apart from the JSON routers because none of it is an API: these are
the addresses a person types or taps, and the assets they carry have to be
versioned so a phone never runs yesterday's script against today's markup.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)

from . import deps, guides, report, resources, users
from .deps import Conn, EventAdmin, FieldAccess

log = logging.getLogger(__name__)

router = APIRouter()

STATIC_DIR = resources.package_file("static")

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


@router.get("/robots.txt")
async def robots() -> PlainTextResponse:
    """Keep every crawler out. Nothing here is meant to be found by
    search: the role pages are bearer links, and a link that gets
    indexed is a link handed to everyone. Pages also carry a noindex
    meta, because robots.txt only asks crawlers not to FETCH a page -
    a URL that reaches a search engine some other way (a shared link,
    a browser extension) can still be listed by address alone."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
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
        conn = deps.connect(request)
        try:
            conn.execute("SELECT 1 FROM event LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        log.exception("Health check could not reach the database")
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok",
                         "version": request.app.state.version})


@router.get("/setup")
async def setup_page(conn: Conn) -> HTMLResponse:
    # First run: nobody exists yet, so the page offers to create the
    # first system administrator instead of asking for a login that
    # could never succeed.
    needs_first_user = not users.any_users(conn)
    html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8")
    return _page(
        html.replace("{{FIRST_RUN}}", "true" if needs_first_user else "false")
    )


# The after-event page for the race lead: pickups counted, notes listed,
# no names. Behind the admin login - it is the club that prints or
# screenshots this and hands it over, not the organizer following a link.
@router.get("/setup/events/{event_id}/report")
async def setup_report(event_id: int, auth: EventAdmin) -> HTMLResponse:
    conn = auth.conn
    # Off the loop: this walks every incident and sighting of the
    # event, and the live map must not pause while the officer reads.
    data = await asyncio.to_thread(report.build, conn, event_id)
    return HTMLResponse(report.render(data),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


# --- pages -------------------------------------------------------------


@router.get("/")
async def index() -> RedirectResponse:
    # No public landing page: the field roles arrive by link and the
    # only thing at the domain itself is the club officer's sign-in. It
    # confirms Course Ops is here, which a 404 with our favicon on it
    # already did; what it must never do is name an event.
    return RedirectResponse("/setup", status_code=302)


@router.get("/e/{event_slug}/{token}")
async def map_page(
    event_slug: str, token: str, auth: FieldAccess
) -> HTMLResponse:
    # The manifest URL carries the token, because the app has no
    # tokenless entry point - a static start_url would install a shortcut
    # to a 404.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    manifest = f"/api/{event_slug}/{token}/manifest.webmanifest"
    return _page(html.replace("__MANIFEST_URL__", manifest))


# --- the guides --------------------------------------------------------

# Unauthenticated on purpose: these are the volunteer guides, public in
# the repository already, and the `?` on a role page has to open without
# asking anyone for anything. They name no event and hold no token. What
# they must not do is leak a path: `guides.load` refuses anything that is
# not a bare page name before touching the filesystem.
@router.get("/help")
@router.get("/help/")
async def help_index() -> HTMLResponse:
    return _guide(guides.INDEX)


@router.get("/help/{page}")
async def help_page(page: str) -> HTMLResponse:
    return _guide(page)


def _guide(name: str) -> HTMLResponse:
    page = guides.load(name)
    if page is None:
        raise HTTPException(status_code=404, detail="No such guide")
    html = guides.page_html(page, guides.page_names())
    return _page(html)

