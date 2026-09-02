"""Admin accounts, passwords and sessions.

Only administrators have accounts. Volunteers on the day still use role links,
which is deliberate: a link can be re-sent to someone whose phone died, at
6am, by anyone holding it, with no account recovery and no admin awake to do it.
Accounts exist because setup is different work - it happens beforehand, by a
named person, and needs to be attributable.

Two roles:

- **system_admin** - everything, including creating events and managing users.
- **event_admin** - only the events they are assigned to, and no user
  management. A club officer who runs one race should not be able to delete
  another club's event when this is hosted.

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import scrypt

ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_EVENT_ADMIN = "event_admin"
ROLES = (ROLE_SYSTEM_ADMIN, ROLE_EVENT_ADMIN)

ROLE_LABELS = {
    ROLE_SYSTEM_ADMIN: "System administrator",
    ROLE_EVENT_ADMIN: "Event administrator",
}

# scrypt parameters. n=2**15 with r=8 costs roughly 32 MB and a few tens of
# milliseconds per hash - unnoticeable on a login, expensive in bulk. Stored
# with each hash so these can be raised later without breaking old passwords.
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


class AuthError(Exception):
    """Login or permission failure. The message is safe to show a user."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str | None
    role: str

    @property
    def is_system_admin(self) -> bool:
        return self.role == ROLE_SYSTEM_ADMIN

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
            "is_system_admin": self.is_system_admin,
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


def check_password_quality(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
            "Length matters far more than punctuation."
        )


# --- accounts ---------------------------------------------------------------

def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    display_name: str | None = None,
) -> User:
    username = normalize_username(username)
    if not username:
        raise AuthError("A username is required.")
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    check_password_quality(password)
    if conn.execute("SELECT 1 FROM user WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"{username!r} already exists.")

    cur = conn.execute(
        "INSERT INTO user (username, password_hash, role, display_name)"
        " VALUES (?, ?, ?, ?)",
        (username, hash_password(password), role,
         (display_name or "").strip() or None),
    )
    return get_user(conn, int(cur.lastrowid))


def get_user(conn: sqlite3.Connection, user_id: int) -> User:
    row = conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("No such user.")
    return User(row["id"], row["username"], row["display_name"], row["role"])


def list_users(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM user ORDER BY role, username"
    ).fetchall():
        user = User(row["id"], row["username"], row["display_name"], row["role"])
        entry = user.as_dict()
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


def may_access_event(conn: sqlite3.Connection, user: User, event_id: int) -> bool:
    """System admins see everything; event admins only what they are assigned.

    This is what makes hosting for several clubs safe: one club's officer must
    not be able to touch another club's event.
    """
    if user.is_system_admin:
        return True
    return event_id in events_for(conn, user.id)


# --- sessions ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User:
    row = conn.execute(
        "SELECT * FROM user WHERE username = ?", (normalize_username(username),)
    ).fetchone()

    # Hash even when the user does not exist, so a missing account and a wrong
    # password take the same time and cannot be told apart.
    stored = row["password_hash"] if row else hash_password("dummy-for-timing")
    ok = verify_password(password or "", stored)

    if row is None or not ok or not row["is_active"]:
        # One message for every failure: which half was wrong is not the
        # attacker's business.
        raise AuthError("Incorrect username or password.")

    conn.execute(
        "UPDATE user SET last_login = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?", (row["id"],)
    )
    return User(row["id"], row["username"], row["display_name"], row["role"])


def start_session(conn: sqlite3.Connection, user_id: int) -> str:
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
    return User(row["id"], row["username"], row["display_name"], row["role"])


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token = ?", (token,))


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM session WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    )
    return cur.rowcount
