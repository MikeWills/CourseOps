"""Admin accounts, passwords and sessions.

Only administrators have accounts. Volunteers on the day still use role links,
which is deliberate: a link can be re-sent to someone whose phone died, at
6am, by anyone holding it, with no account recovery and no admin awake to do it.
Accounts exist because setup is different work - it happens beforehand, by a
named person, and needs to be attributable.

Three roles, matching how this is actually run:

- **system_admin** - the person hosting the service. Sees every organization,
  and belongs to none.
- **org_admin** - a club officer. Creates and runs events for their own club,
  and manages that club's administrators. Cannot see another club at all.
- **event_admin** - assigned to specific events within their club. Useful when
  someone sets up one race but should not be able to delete the others.

The organization is the tenancy boundary. Without it, hosting for a second club
would mean either trusting every officer with every event, or the host doing all
setup by hand.

Passwords are hashed with scrypt from the standard library: memory-hard, so a
stolen database resists GPU cracking far better than a plain SHA, and no third
party dependency to audit. Every hash carries its own salt and its own
parameters, so the cost can be raised later without invalidating existing
passwords.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import scrypt

from . import db

ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_ORG_ADMIN = "org_admin"
ROLE_EVENT_ADMIN = "event_admin"
ROLES = (ROLE_SYSTEM_ADMIN, ROLE_ORG_ADMIN, ROLE_EVENT_ADMIN)

ROLE_LABELS = {
    ROLE_SYSTEM_ADMIN: "System administrator",
    ROLE_ORG_ADMIN: "Organization administrator",
    ROLE_EVENT_ADMIN: "Event administrator",
}

# scrypt parameters. n=2**15 with r=8 costs roughly 32 MB and about a third
# of a second per hash (measured 0.25-0.36 s on a desktop; a VPS core is no
# faster) - unnoticeable on a login, expensive in bulk. That cost is the
# point, and it is also why the web layer runs every hash in a worker thread
# and refuses repeated failures: a third of a second on the event loop is a
# third of a second in which no phone in the field receives a position.
# Stored with each hash so these can be raised later without breaking old
# passwords.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16

# 128 * n * r is ~32 MB at these parameters, which is exactly OpenSSL's default
# cap, so it refuses. Raised explicitly rather than weakening the parameters:
# the memory hardness is the whole point of choosing scrypt.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

# Long enough that guessing is hopeless. Sessions live in the database so they
# can be revoked; a signed cookie could not be.
SESSION_BYTES = 32
SESSION_DAYS = 30

# Minimum that is worth enforcing without being theatre. Length beats
# composition rules, which mostly produce Password1! and a sticky note.
MIN_PASSWORD_LENGTH = 10

# How many failed sign-ins one username, or one address, gets per window
# before the next attempt is refused outright. Five is enough for a person
# who has forgotten which password they used and nowhere near enough for a
# guesser. The window is short because the person who matters is the officer
# locked out at 6am on race morning: a minute is an annoyance, an hour is a
# phone call.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60


class AuthError(Exception):
    """Login or permission failure. The message is safe to show a user."""


def _text(value: object, limit: int | None = None) -> str | None:
    """Free text from a body, as an AuthError when it is not text at all -
    the one exception every caller of this module already handles."""
    try:
        return db.clean_text(value, limit)
    except ValueError as exc:
        raise AuthError(str(exc)) from None


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str | None
    role: str
    organization_id: int | None = None

    @property
    def is_system_admin(self) -> bool:
        return self.role == ROLE_SYSTEM_ADMIN

    @property
    def is_org_admin(self) -> bool:
        return self.role == ROLE_ORG_ADMIN

    @property
    def may_manage_users(self) -> bool:
        """System admins manage anyone; org admins manage their own club."""
        return self.role in (ROLE_SYSTEM_ADMIN, ROLE_ORG_ADMIN)

    @property
    def may_create_events(self) -> bool:
        return self.role in (ROLE_SYSTEM_ADMIN, ROLE_ORG_ADMIN)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": self.role_label,
            "organization_id": self.organization_id,
            "is_system_admin": self.is_system_admin,
            "is_org_admin": self.is_org_admin,
            "may_manage_users": self.may_manage_users,
            "may_create_events": self.may_create_events,
        }


# --- password hashing -------------------------------------------------------

def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all hex. Self-describing so the cost can change."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = scrypt(password.encode("utf-8"), salt=salt,
                    n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                    maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)),
            # Sized from the stored parameters, so a hash written with a higher
            # cost still verifies after the defaults change.
            maxmem=128 * int(n) * int(r) * 2,
        )
    except (ValueError, TypeError):
        return False
    # compare_digest, not ==, so a wrong password cannot be found byte by byte
    # from response timing.
    return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))


# The hash that a sign-in with an unknown username is checked against, so the
# missing-account and wrong-password paths cost exactly one scrypt each.
#
# It used to be `hash_password("dummy-for-timing")` computed inline, followed
# by the verify - two hashes for a name that does not exist against one for a
# name that does. Half a second against a quarter is a gap that survives any
# network jitter, so the "same time" the comment promised was in fact a list
# of which officers have accounts. Computed once, lazily: paying a third of a
# second at import would put it on every CLI invocation, including --help.
_dummy_hash_cache: str | None = None


def dummy_hash() -> str:
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = hash_password(secrets.token_hex(16))
    return _dummy_hash_cache


class LoginLimiter:
    """In-memory count of recent sign-in failures, per key.

    Keyed by whatever the caller passes - the web layer uses the username
    AND the client address, so a guesser cycling names is stopped by the
    address and one cycling addresses is stopped by the name. Memory only:
    it exists to keep scrypt off the loop under a flood, and a restart
    forgetting the counts costs nothing. One instance per application, so a
    test suite's many sign-ins never throttle each other.

    `clock` is injectable for tests; the default is monotonic so a clock
    change on the club laptop cannot lock anyone out for longer than the
    window.
    """

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES,
                 window_seconds: float = LOGIN_WINDOW_SECONDS,
                 clock=time.monotonic) -> None:
        self.max_failures = max_failures
        self.window = float(window_seconds)
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        now = self._clock()
        hits = self._failures.get(key)
        if hits is None:
            return deque()
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if not hits:
            # Drop the key rather than keep an empty deque per address the
            # internet has ever tried, or the map fills with them.
            del self._failures[key]
            return deque()
        return hits

    def retry_after(self, *keys: str) -> int | None:
        """Seconds until the next attempt is allowed, or None if it is now."""
        wait = 0.0
        for key in keys:
            hits = self._recent(key)
            if len(hits) >= self.max_failures:
                wait = max(wait, hits[0] + self.window - self._clock())
        if wait <= 0:
            return None
        return max(1, int(wait + 0.999))

    def failed(self, *keys: str) -> None:
        now = self._clock()
        for key in keys:
            self._failures.setdefault(key, deque()).append(now)

    def succeeded(self, *keys: str) -> None:
        """A correct password clears the slate: the person who mistyped four
        times and then got it right is not a guesser, and must not be left
        one slip from a lockout for the next minute."""
        for key in keys:
            self._failures.pop(key, None)


def check_password_quality(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
            "Length matters far more than punctuation."
        )


# --- accounts ---------------------------------------------------------------

def normalize_username(username: str) -> str:
    return (_text(username) or "").lower()


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    display_name: str | None = None,
    organization_id: int | None = None,
) -> User:
    username = normalize_username(username)
    if not username:
        raise AuthError("A username is required.")
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    check_password_quality(password)
    if conn.execute("SELECT 1 FROM user WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"{username!r} already exists.")

    if role != ROLE_SYSTEM_ADMIN and organization_id is None:
        raise AuthError(
            "Only a system administrator can exist outside an organization."
        )

    cur = conn.execute(
        "INSERT INTO user (username, password_hash, role, display_name,"
        " organization_id) VALUES (?, ?, ?, ?, ?)",
        (username, hash_password(password), role,
         _text(display_name, 80),
         None if role == ROLE_SYSTEM_ADMIN else organization_id),
    )
    return get_user(conn, int(cur.lastrowid))


def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["username"], row["display_name"], row["role"],
                row["organization_id"])


def get_user(conn: sqlite3.Connection, user_id: int) -> User:
    row = conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("No such user.")
    return _user(row)


def list_users(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM user ORDER BY role, username"
    ).fetchall():
        entry = _user(row).as_dict()
        entry["is_active"] = bool(row["is_active"])
        entry["last_login"] = row["last_login"]
        entry["events"] = events_for(conn, row["id"])
        out.append(entry)
    return out


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    check_password_quality(password)
    conn.execute("UPDATE user SET password_hash = ? WHERE id = ?",
                 (hash_password(password), user_id))
    # Every existing session is invalidated: a password change usually means it
    # was compromised, and leaving old sessions alive would defeat the point.
    conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))


def set_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute("UPDATE user SET is_active = ? WHERE id = ?",
                 (1 if active else 0, user_id))
    if not active:
        conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM user WHERE id = ?", (user_id,))


def list_organizations(conn: sqlite3.Connection) -> list[dict]:
    events = {r[0]: r[1] for r in conn.execute(
        "SELECT organization_id, COUNT(*) FROM event GROUP BY organization_id"
    ).fetchall()}
    admins = {r[0]: r[1] for r in conn.execute(
        "SELECT organization_id, COUNT(*) FROM user GROUP BY organization_id"
    ).fetchall()}
    out = []
    for row in conn.execute(
        "SELECT * FROM organization ORDER BY name"
    ).fetchall():
        entry = {key: row[key] for key in row.keys()}
        entry["event_count"] = events.get(row["id"], 0)
        entry["admin_count"] = admins.get(row["id"], 0)
        out.append(entry)
    return out


def create_organization(conn: sqlite3.Connection, slug: str, name: str,
                        contact: str | None = None) -> dict:
    slug = (_text(slug) or "").lower()
    name = _text(name) or ""
    if not slug or not name:
        raise AuthError("An organization needs a short name and a full name.")
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise AuthError("The short name may only contain letters, numbers, - and _.")
    if conn.execute("SELECT 1 FROM organization WHERE slug = ?", (slug,)).fetchone():
        raise AuthError(f"An organization called {slug!r} already exists.")
    cur = conn.execute(
        "INSERT INTO organization (slug, name, contact) VALUES (?, ?, ?)",
        (slug, name, _text(contact, 200)),
    )
    row = conn.execute("SELECT * FROM organization WHERE id = ?",
                       (int(cur.lastrowid),)).fetchone()
    return {key: row[key] for key in row.keys()}


def count_system_admins(conn: sqlite3.Connection, exclude_id: int | None = None) -> int:
    query = ("SELECT COUNT(*) FROM user WHERE role = ? AND is_active = 1")
    params: list = [ROLE_SYSTEM_ADMIN]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(query, params).fetchone()[0]


def any_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM user LIMIT 1").fetchone() is not None


# --- event assignment -------------------------------------------------------

def events_for(conn: sqlite3.Connection, user_id: int) -> list[int]:
    return [
        row["event_id"] for row in conn.execute(
            "SELECT event_id FROM user_event WHERE user_id = ?", (user_id,)
        ).fetchall()
    ]


def set_events(conn: sqlite3.Connection, user_id: int, event_ids: list[int]) -> None:
    conn.execute("DELETE FROM user_event WHERE user_id = ?", (user_id,))
    for event_id in event_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_event (user_id, event_id) VALUES (?, ?)",
            (user_id, event_id),
        )


def update_organization(conn: sqlite3.Connection, organization_id: int,
                        payload: dict) -> dict:
    """Change a club's display name or contact.

    The slug is deliberately not editable. It is not shown to volunteers and
    changing it buys nothing, while every mistake it could fix is cosmetic.
    """
    fields, values = [], []
    if "name" in payload:
        name = _text(payload.get("name"))
        if not name:
            raise AuthError("An organization needs a full name.")
        fields.append("name = ?")
        values.append(name)
    if "contact" in payload:
        fields.append("contact = ?")
        values.append(_text(payload.get("contact"), 200))
    if not fields:
        raise AuthError("Nothing to change.")

    values.append(organization_id)
    cur = conn.execute(
        f"UPDATE organization SET {', '.join(fields)} WHERE id = ?", values
    )
    if cur.rowcount == 0:
        raise AuthError(f"No organization with id {organization_id}.")
    row = conn.execute(
        "SELECT * FROM organization WHERE id = ?", (organization_id,)
    ).fetchone()
    return dict(row)


def organization_of_event(conn: sqlite3.Connection, event_id: int) -> int | None:
    row = conn.execute(
        "SELECT organization_id FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    return row["organization_id"] if row else None


def may_access_event(conn: sqlite3.Connection, user: User, event_id: int) -> bool:
    """Who may touch an event.

    The single place this is decided, so widening or narrowing access later is a
    change here rather than in every endpoint.

    - system_admin: everything.
    - org_admin: every event belonging to their club.
    - event_admin: only events explicitly assigned to them, AND only within
      their own club - the second half matters, because an assignment left
      behind after someone moves club must not become a way in.
    """
    if user.is_system_admin:
        return True

    owner = organization_of_event(conn, event_id)
    if owner is None or user.organization_id is None:
        return False
    if owner != user.organization_id:
        return False

    if user.is_org_admin:
        return True
    return event_id in events_for(conn, user.id)


def may_manage_user(conn: sqlite3.Connection, actor: User, target: User) -> bool:
    """An org admin manages only their own club's administrators."""
    if actor.is_system_admin:
        return True
    if not actor.is_org_admin:
        return False
    # And never another system administrator.
    if target.is_system_admin:
        return False
    return (target.organization_id is not None
            and target.organization_id == actor.organization_id)


# --- sessions ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User:
    row = conn.execute(
        "SELECT * FROM user WHERE username = ?", (normalize_username(username),)
    ).fetchone()

    # Verify against a real hash even when the user does not exist, so a
    # missing account and a wrong password cost one scrypt each and cannot be
    # told apart by the clock. See dummy_hash for what went wrong before.
    stored = row["password_hash"] if row else dummy_hash()
    ok = verify_password(password or "", stored)

    if row is None or not ok or not row["is_active"]:
        # One message for every failure: which half was wrong is not the
        # attacker's business.
        raise AuthError("Incorrect username or password.")

    conn.execute(
        "UPDATE user SET last_login = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?", (row["id"],)
    )
    return _user(row)


def start_session(conn: sqlite3.Connection, user_id: int) -> str:
    # Every sign-in adds a row and nothing else ever removed the expired
    # ones: resolve_session deletes a stale row only when that exact token
    # is presented again, which a browser that has forgotten it never does.
    # One DELETE per login keeps the table the size of the live sessions.
    purge_expired_sessions(conn)
    token = secrets.token_urlsafe(SESSION_BYTES)
    expires = (_now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires),
    )
    return token


def resolve_session(conn: sqlite3.Connection, token: str) -> User | None:
    if not token:
        return None
    row = conn.execute(
        """
        SELECT s.token, s.expires_at, u.*
          FROM session s
          JOIN user u ON u.id = s.user_id
         WHERE s.token = ? AND u.is_active = 1
        """,
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now().strftime("%Y-%m-%dT%H:%M:%SZ"):
        conn.execute("DELETE FROM session WHERE token = ?", (token,))
        return None

    conn.execute(
        "UPDATE session SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE token = ?", (token,)
    )
    return _user(row)


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token = ?", (token,))


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM session WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    )
    return cur.rowcount
