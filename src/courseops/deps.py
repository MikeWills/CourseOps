"""Who is asking, and the connection they get: the FastAPI dependencies.

Every route that touches the database says what it needs in its signature -
a valid role link, a link holding one capability, a signed-in administrator,
an administrator who may touch this event - and receives the request's
connection with the answer. The dependency opens the connection, yields it,
and closes it when the response is done, whichever way the route ended.
That is what replaced a hundred hand-written `conn.close()` calls, several
of which missed an error path.

The refusals live here and nowhere else, and their shape is deliberate:

- A role link that does not resolve is 404, never 403. A 403 would confirm
  the event exists to someone holding a token that is not theirs.
- A valid link lacking a capability is 403 with the reason: the holder knows
  the event exists, and "read-only" is the answer they need.
- No session is 401; a session that may not do this is 403; an event that
  does not exist is 404 before the access check, so a stale bookmark to a
  deleted event is a message rather than a traceback.

Every dependency is `async def` on purpose. A synchronous generator would be
run in a worker thread, and the connection would then be opened and closed
off the loop while the route used it on the loop; the routes work the
database on the loop today, and this changes nothing about that. A route
that wants a heavy read off the loop hands the connection to
`asyncio.to_thread` itself and is its only user meanwhile.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, AsyncIterator, NamedTuple

from fastapi import Depends, HTTPException, Request

from . import access, db, users

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


def connect(request: Request) -> sqlite3.Connection:
    # SQLite connections are not shareable across threads; one per request
    # is cheap for this workload and avoids the whole question.
    return db.connect(request.app.state.settings.db_path)


async def connection(request: Request) -> AsyncIterator[sqlite3.Connection]:
    """A connection and nothing else, for the routes that need no one to be
    anyone: the setup page deciding whether to offer first-run, the session
    check, sign-out."""
    conn = connect(request)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(connection)]


# --- role links (the field app) --------------------------------------------

class Grant(NamedTuple):
    """A resolved role link: the request's connection and what the link may do."""
    conn: sqlite3.Connection
    granted: access.Access


async def field_access(request: Request, event_slug: str,
                       token: str) -> AsyncIterator[Grant]:
    conn = connect(request)
    try:
        granted = access.resolve(conn, event_slug, token)
        if granted is None:
            # 404, not 403: an invalid token must not confirm the event exists.
            raise HTTPException(status_code=404, detail="Not found")
        yield Grant(conn, granted)
    finally:
        conn.close()


FieldAccess = Annotated[Grant, Depends(field_access)]


def needs(capability: str):
    """A role link that holds `capability`; 403 with the reason otherwise.

    Every mutation names the capability it needs this way and goes through
    one check, so widening a role is a change to access.ROLE_CAPABILITIES
    rather than a rewrite of each endpoint.
    """
    async def dependency(grant: FieldAccess) -> Grant:
        granted = grant.granted
        if not granted.can(capability):
            # 403 here, not 404: the token is valid and its holder knows the
            # event exists. Hiding the reason would just be confusing.
            detail = (
                f"{granted.role_label} is read-only."
                if not granted.can_write
                else f"{granted.role_label} cannot change that."
            )
            raise HTTPException(status_code=403, detail=detail)
        return grant

    return dependency


# --- administrator sessions ------------------------------------------------

class Admin(NamedTuple):
    """A signed-in administrator: the request's connection and who they are."""
    conn: sqlite3.Connection
    user: users.User


def session_token(request: Request) -> str:
    # Either name: a browser that reached us over HTTPS holds the
    # prefixed cookie, one on plain HTTP the bare one.
    return (request.cookies.get(SECURE_SESSION_COOKIE)
            or request.cookies.get(SESSION_COOKIE, ""))


def current_user(request: Request, conn: sqlite3.Connection) -> users.User | None:
    return users.resolve_session(conn, session_token(request))


async def signed_in(request: Request) -> AsyncIterator[Admin]:
    conn = connect(request)
    try:
        user = current_user(request, conn)
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        yield Admin(conn, user)
    finally:
        conn.close()


SignedIn = Annotated[Admin, Depends(signed_in)]


async def user_manager(admin: SignedIn) -> Admin:
    """System admins and org admins both manage people, at different scopes."""
    if not admin.user.may_manage_users:
        raise HTTPException(
            status_code=403, detail="You cannot manage administrators."
        )
    return admin


async def event_creator(admin: SignedIn) -> Admin:
    if not admin.user.may_create_events:
        raise HTTPException(
            status_code=403, detail="You cannot create events."
        )
    return admin


async def system_admin(admin: SignedIn) -> Admin:
    if not admin.user.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail="Only a system administrator can do that.",
        )
    return admin


async def event_admin(admin: SignedIn, event_id: int) -> Admin:
    """Every event-scoped setup route goes through here.

    One place decides whether a user may touch an event, so widening or
    narrowing access later is a change to may_access_event rather than to
    every endpoint.
    """
    conn, user = admin
    # Existence first: may_access_event says yes to a system admin before
    # looking the event up, and a stale bookmark to a deleted event's
    # setup page was then a traceback from whichever route dereferenced
    # the missing row. For anyone else the answer is 403 either way, so
    # nothing is confirmed that was not already.
    if conn.execute("SELECT 1 FROM event WHERE id = ?",
                    (event_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="No such event.")
    if not users.may_access_event(conn, user, event_id):
        raise HTTPException(status_code=403, detail="Not your event.")
    return admin


UserManager = Annotated[Admin, Depends(user_manager)]
EventCreator = Annotated[Admin, Depends(event_creator)]
SystemAdmin = Annotated[Admin, Depends(system_admin)]
EventAdmin = Annotated[Admin, Depends(event_admin)]


# --- request bodies --------------------------------------------------------

async def raw_body(request: Request) -> bytes:
    """The request body, capped at MAX_JSON_BYTES, or 413."""
    # Read in chunks and counted, so a chunked body with no
    # Content-Length - which the middleware cannot size - is still cut
    # off at the cap rather than buffered whole.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BYTES:
            raise HTTPException(status_code=413,
                                detail="Request body too large.")
        chunks.append(chunk)
    return b"".join(chunks)


async def json_body(request: Request) -> dict:
    """The request's JSON object, capped, or a 4xx that says what was wrong."""
    try:
        body = json.loads(await raw_body(request))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Expected a JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return body
