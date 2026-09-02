"""Role-based access tokens.

There are no user accounts in v1. Each event gets one long random URL per role,
which a club officer pastes into the right group text. That is a bearer token
and no stronger than the message carrying it - which is the right trade here:
the data is operational, not sensitive, and it means a volunteer whose phone
died can be re-admitted by re-sending a link, with no admin to call at 6am on
race morning.

What the token gates is real, though: NCS can write, Liaison cannot.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

# 32 url-safe characters, ~192 bits. Long enough that guessing is hopeless,
# short enough to survive being pasted into a text message.
TOKEN_BYTES = 24

ROLE_NCS = "ncs"
ROLE_LIAISON = "liaison"
ROLE_LOGISTICS = "logistics"
ROLES = (ROLE_NCS, ROLE_LIAISON, ROLE_LOGISTICS)

# Liaison and Logistics are different teams doing different jobs, and each gets
# its own link so one can be revoked without cutting off the other:
#   Liaison   - embedded with Public Safety and Medics
#   Logistics - out on the course: traffic control, cone placement, teardown
ROLE_LABELS = {
    ROLE_NCS: "Net Control",
    ROLE_LIAISON: "Liaison",
    ROLE_LOGISTICS: "Logistics",
}

# Only NCS writes in v1; both field roles are read-only.
WRITE_ROLES = (ROLE_NCS,)


@dataclass(frozen=True)
class Access:
    event_id: int
    event_slug: str
    role: str
    token: str

    @property
    def can_write(self) -> bool:
        """Only NCS writes in v1.

        Every mutation goes through a single server-side check regardless of
        role, so granting Liaison write access later is a permission change
        rather than a rewrite.
        """
        return self.role in WRITE_ROLES

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


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
