"""Role-based access tokens.

There are no user accounts in v1. Each event gets one long random URL per role,
which a club officer pastes into the right group text. That is a bearer token
and no stronger than the message carrying it - which is the right trade here:
the data is operational, not sensitive, and it means a volunteer whose phone
died can be re-admitted by re-sending a link, with no admin to call at 6am on
race morning.

What the token gates is real, though: every role can report what it sees,
and only NCS and SAG can work the pickup queue.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

# 32 url-safe characters, ~192 bits. Long enough that guessing is hopeless,
# short enough to survive being pasted into a text message.
TOKEN_BYTES = 24

ROLE_NCS = "ncs"
ROLE_SAG = "sag"
ROLE_LIAISON = "liaison"
ROLE_LOGISTICS = "logistics"
ROLES = (ROLE_NCS, ROLE_SAG, ROLE_LIAISON, ROLE_LOGISTICS)

# Four teams doing four different jobs, each with its own link so one can be
# revoked without cutting off the others:
#   Net Control - runs the net
#   SAG         - drives the course collecting runners who cannot continue
#   Liaison     - embedded with Public Safety and Medics
#   Logistics   - out on the course: traffic control, cone placement, teardown
ROLE_LABELS = {
    ROLE_NCS: "Net Control",
    ROLE_SAG: "SAG",
    ROLE_LIAISON: "Liaison",
    ROLE_LOGISTICS: "Logistics",
}

# --- capabilities -----------------------------------------------------------
#
# Write access used to be one flag, which was fine while NCS was the only role
# that wrote anything. SAG breaks that: a driver needs to move a pickup along
# its workflow and must not be able to revoke a link or rewrite the roster.
#
# So permission is per capability rather than per role. Each mutating endpoint
# names the capability it needs, in one place, and a role is a set of them.
# Widening a role stays a change to this table rather than to the endpoints.

# Reporting is split from managing, because they are different jobs done by
# different people. Anyone out on the course can SEE a runner sitting down or a
# blocked intersection - that is the whole reason four teams are on the course -
# and a report that has to be relayed over the radio to someone with the right
# link is a report that arrives late or not at all.
#
# Working the queue is a different thing: dispatching, delivering and clearing
# a pickup is NCS and SAG. So a field role can add to the board and describe
# what it found, and cannot close or delete anything. The blast radius of a
# bearer link lost in a parking lot is a spurious pin, not a queue quietly
# emptied of people still waiting.
CAP_INCIDENT_REPORT = "incident_report"  # open a pickup or note, fill it in
CAP_INCIDENTS = "incidents"   # move a pickup along its workflow, delete one
CAP_STATIONS = "stations"     # a roster entry's operational status
CAP_SSID = "ssid"             # adopt or dismiss an unexpected SSID
CAP_LEADERS = "leaders"       # lead runner sightings
CAP_COURSE = "course"         # bib colours and course styling

ALL_CAPABILITIES = frozenset(
    {CAP_INCIDENT_REPORT, CAP_INCIDENTS, CAP_STATIONS, CAP_SSID, CAP_LEADERS,
     CAP_COURSE}
)

ROLE_CAPABILITIES = {
    ROLE_NCS: ALL_CAPABILITIES,
    # A SAG driver marks a pickup en route, picked up and dropped off, and
    # fills in the bib once they have the runner in front of them. Nothing
    # else: this is a bearer link in a moving vehicle, and the blast radius of
    # a lost phone should be one incident queue, not the whole event.
    ROLE_SAG: frozenset({CAP_INCIDENT_REPORT, CAP_INCIDENTS}),
    # Liaison and Logistics report and no more. Both are where the incidents
    # happen: Logistics is on the course at a cone or an intersection, and
    # Liaison sits with Public Safety and Medics, taking pickups that come in
    # by another route entirely. Liaison is not AT the thing they are
    # reporting, which is why dropping a pin on the map stays the primary way
    # in and locating yourself is only ever the shortcut beside it.
    ROLE_LIAISON: frozenset({CAP_INCIDENT_REPORT}),
    ROLE_LOGISTICS: frozenset({CAP_INCIDENT_REPORT}),
}

# Kept for the roster filter and anything asking the old yes/no question.
WRITE_ROLES = tuple(
    role for role, caps in ROLE_CAPABILITIES.items() if caps
)


@dataclass(frozen=True)
class Access:
    event_id: int
    event_slug: str
    role: str
    token: str

    @property
    def capabilities(self) -> frozenset[str]:
        return ROLE_CAPABILITIES.get(self.role, frozenset())

    def can(self, capability: str) -> bool:
        """Whether this role may perform one kind of mutation.

        The single server-side check every mutating endpoint goes through.
        """
        return capability in self.capabilities

    @property
    def can_write(self) -> bool:
        """Whether this role may change anything at all.

        Only useful for deciding whether to show a role the write affordances
        in general; anything specific must ask `can()` for its capability.
        """
        return bool(self.capabilities)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


# --- server-wide setup access ----------------------------------------------
#
# Creating the first event needs a credential that is not tied to an event, so
# this cannot reuse the per-event tokens above.


def ensure_admin_token(conn: sqlite3.Connection) -> str:
    """The setup link, created once and reused across restarts.

    Stable on purpose: a token that changed every start would either have to be
    re-copied constantly or emailed around, and neither is better.
    """
    row = conn.execute(
        "SELECT token FROM admin_token WHERE revoked = 0 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["token"]
    token = generate_token()
    conn.execute("INSERT INTO admin_token (token, label) VALUES (?, ?)",
                 (token, "setup"))
    return token


def resolve_admin(conn: sqlite3.Connection, token: str) -> bool:
    """True if this is a live setup token."""
    if not token:
        return False
    row = conn.execute(
        "SELECT id FROM admin_token WHERE token = ? AND revoked = 0", (token,)
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE admin_token SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?",
        (row["id"],),
    )
    return True


def rotate_admin_token(conn: sqlite3.Connection) -> str:
    """Revoke every setup token and issue a new one."""
    conn.execute("UPDATE admin_token SET revoked = 1 WHERE revoked = 0")
    return ensure_admin_token(conn)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def create_token(
    conn: sqlite3.Connection, event_id: int, role: str, label: str | None = None
) -> str:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}. Use one of {', '.join(ROLES)}.")
    token = generate_token()
    conn.execute(
        "INSERT INTO access_token (event_id, token, role, label) VALUES (?, ?, ?, ?)",
        (event_id, token, role, label),
    )
    return token


def tokens_for_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM access_token WHERE event_id = ? ORDER BY role, id",
        (event_id,),
    ).fetchall()


def ensure_tokens(conn: sqlite3.Connection, event_id: int) -> dict[str, str]:
    """Return one live token per role, creating any that are missing."""
    existing = {
        row["role"]: row["token"]
        for row in tokens_for_event(conn, event_id)
        if not row["revoked"]
    }
    for role in ROLES:
        if role not in existing:
            existing[role] = create_token(conn, event_id, role)
    return existing


def revoke(conn: sqlite3.Connection, token_id: int) -> bool:
    cur = conn.execute(
        "UPDATE access_token SET revoked = 1 WHERE id = ?", (token_id,)
    )
    return cur.rowcount > 0


def resolve(
    conn: sqlite3.Connection, event_slug: str, token: str
) -> Access | None:
    """Look up a token, scoped to the event in the URL.

    Both must match: a token is meaningless against another event even if it is
    otherwise valid, which keeps events isolated once this is hosted for more
    than one club.
    """
    if not token:
        return None
    row = conn.execute(
        """
        SELECT t.token, t.role, e.id AS event_id, e.slug
        FROM access_token t
        JOIN event e ON e.id = t.event_id
        WHERE t.token = ? AND e.slug = ? AND t.revoked = 0
        """,
        (token, event_slug),
    ).fetchone()
    if row is None:
        return None

    conn.execute(
        "UPDATE access_token SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE token = ?",
        (token,),
    )
    return Access(
        event_id=row["event_id"],
        event_slug=row["slug"],
        role=row["role"],
        token=row["token"],
    )
