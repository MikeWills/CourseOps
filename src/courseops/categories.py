"""What kinds of place and what kinds of station this event has.

Two taxonomies, deliberately treated differently.

**POI categories are open.** A KML arrives with whatever layers the organizer
drew — mile markers, medical, aid stations, traffic control, portable toilets,
spectator zones — and the next club will have a different set. Hardcoding the
list means editing Python to accept a race, which is the opposite of what this
project is for. So a club may add as many as they like, name them in their own
words, pick an icon and a colour, and turn each on and off as a map layer.

The load-bearing property is `staffed`: **we put a person here and track them.**
That is what separates an aid station from a portable toilet, and it is what
decides which categories get the operational treatment — an operator posted to
them, a lead runner sighted at them, a What3Words address worth maintaining.
Before this existed the code asked `poi_type == 'aid_station'`, which meant a
club renaming their layer to "Water Stops" silently lost lead runner tracking.

**Station roles are a fixed set with open names.** Each key carries its own
status vocabulary — an aid station is "Torn down" where a sweep is "Finished" —
and that mapping lives in code. But one club's "Rover" is another's "Floater",
so the displayed name belongs to the club. Renaming is safe precisely because
nothing keys off the name.
"""

from __future__ import annotations

import re
import sqlite3

# Seeded into every new event. Not a limit — a starting point a club edits.
# `staffed` is off for medical by default: a medic tent is run by the race's own
# medical team, not by an operator we are tracking. A club that does staff them
# ticks the box.
DEFAULT_POI_CATEGORIES: list[tuple[str, str, bool, str, str]] = [
    # key, name, staffed, icon, color
    ("aid_station", "Aid station", True, "cup", "#0072b2"),
    ("start", "Start", True, "flag", "#009e73"),
    ("finish", "Finish", True, "flag", "#009e73"),
    ("start_finish", "Start / finish", True, "flag", "#009e73"),
    ("medical", "Medical", False, "cross", "#b3261e"),
    ("parking", "Parking", False, "car", "#5a6572"),
    ("other", "Other", False, "pin", "#6b5ea8"),
]

# Labels default on exactly where a person is standing. Those are the pins
# someone needs to tell apart mid-net; a fifty-marker mile layer with labels on
# is the wall of digits this flag exists to prevent.
def _labels_default(staffed: bool) -> int:
    return int(staffed)

# The station roles. Fixed keys, because each has its own status wording in
# db.OP_STATUS_LABELS; the names here are only defaults.
DEFAULT_ROSTER_ROLES: list[tuple[str, str]] = [
    ("net_control", "Net control"),
    ("aid_station", "Aid station"),
    ("sweep", "Sweep"),
    ("sag", "SAG"),
    ("shadow", "Shadow"),
    ("rover", "Rover"),
    ("start_finish", "Start / finish"),
]

_KEY_OK = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")


class CategoryError(ValueError):
    """Rejected input. The message is safe to show a user."""


def slugify(name: str) -> str:
    """A stable key from a human name: "Traffic control" -> traffic_control".

    The key is what `poi.poi_type` stores, so it must never change when the
    name is edited — see `rename_poi_category`, which deliberately leaves it
    alone.
    """
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return key[:40]


# --- POI categories ---------------------------------------------------------

def seed_poi_categories(conn: sqlite3.Connection, event_id: int) -> None:
    """Give a new event the usual starting set, once.

    Also called lazily for events created before categories existed.

    The defaults are seeded ONLY into an event that has none at all. Re-seeding
    every time would resurrect a layer the club deliberately deleted - they
    would remove "Parking", and it would silently reappear on the next page
    load, which is the sort of thing that makes someone stop trusting a screen.
    """
    has_any = conn.execute(
        "SELECT 1 FROM poi_category WHERE event_id = ? LIMIT 1", (event_id,)
    ).fetchone()
    if has_any is None:
        for order, (key, name, staffed, icon, color) in enumerate(
            DEFAULT_POI_CATEGORIES
        ):
            conn.execute(
                """
                INSERT INTO poi_category
                    (event_id, key, name, staffed, icon, color, sort_order,
                     visible, show_labels)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (event_id, key, name, int(staffed), icon, color, order * 10,
                 _labels_default(staffed)),
            )
    # Unlike the defaults, this runs every time: a place whose layer does not
    # exist is in the database, off the map, with no error to say so. Better an
    # unnamed layer the club can rename than a place nobody can see.
    for row in conn.execute(
        "SELECT DISTINCT poi_type FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall():
        key = (row["poi_type"] or "").strip()
        if not key:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO poi_category
                (event_id, key, name, staffed, icon, color, sort_order,
                 visible, show_labels)
            VALUES (?, ?, ?, 0, 'pin', NULL, 900, 1, 0)
            """,
            (event_id, key, key.replace("_", " ").title()),
        )


def poi_categories(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    seed_poi_categories(conn, event_id)
    return conn.execute(
        "SELECT * FROM poi_category WHERE event_id = ?"
        " ORDER BY sort_order, name",
        (event_id,),
    ).fetchall()


def staffed_keys(conn: sqlite3.Connection, event_id: int) -> set[str]:
    """The categories where a person stands.

    Everything operational keys off this rather than a hardcoded
    'aid_station': who can be posted somewhere, where a lead runner can be
    sighted, which places are worth a What3Words address.
    """
    rows = conn.execute(
        "SELECT key FROM poi_category WHERE event_id = ? AND staffed = 1",
        (event_id,),
    ).fetchall()
    return {row["key"] for row in rows}


def add_poi_category(
    conn: sqlite3.Connection,
    event_id: int,
    name: str,
    staffed: bool = False,
    icon: str = "pin",
    color: str | None = None,
) -> sqlite3.Row:
    name = (name or "").strip()
    if not name:
        raise CategoryError("A layer needs a name.")
    key = slugify(name)
    if not _KEY_OK.match(key):
        raise CategoryError(f"{name!r} does not make a usable layer name.")
    existing = conn.execute(
        "SELECT 1 FROM poi_category WHERE event_id = ? AND key = ?",
        (event_id, key),
    ).fetchone()
    if existing is not None:
        raise CategoryError(f"A layer called {name!r} already exists.")

    top = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) AS m FROM poi_category WHERE event_id = ?",
        (event_id,),
    ).fetchone()["m"]
    conn.execute(
        """
        INSERT INTO poi_category
            (event_id, key, name, staffed, icon, color, sort_order,
             visible, show_labels)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (event_id, key, name, int(staffed), icon or "pin", color, top + 10,
         _labels_default(staffed)),
    )
    return get_poi_category(conn, event_id, key)


def get_poi_category(
    conn: sqlite3.Connection, event_id: int, key: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM poi_category WHERE event_id = ? AND key = ?",
        (event_id, key),
    ).fetchone()
    if row is None:
        raise CategoryError(f"No layer {key!r} in this event.")
    return row


def update_poi_category(
    conn: sqlite3.Connection, event_id: int, key: str, payload: dict
) -> sqlite3.Row:
    """Rename or restyle a layer. The key never moves.

    `poi.poi_type` holds the key, so renaming is a display change only and no
    POI has to be touched. That is the whole reason the two are separate.
    """
    get_poi_category(conn, event_id, key)
    fields, values = [], []

    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise CategoryError("A layer needs a name.")
        fields.append("name = ?")
        values.append(name)
    for flag in ("staffed", "visible", "show_labels"):
        if flag in payload:
            fields.append(f"{flag} = ?")
            values.append(int(bool(payload[flag])))
    if "icon" in payload:
        fields.append("icon = ?")
        values.append((payload.get("icon") or "pin").strip() or "pin")
    if "color" in payload:
        fields.append("color = ?")
        values.append((payload.get("color") or "").strip() or None)
    if "sort_order" in payload and payload["sort_order"] is not None:
        fields.append("sort_order = ?")
        values.append(int(payload["sort_order"]))
    if not fields:
        raise CategoryError("Nothing to change.")

    values.extend([event_id, key])
    conn.execute(
        f"UPDATE poi_category SET {', '.join(fields)}"
        " WHERE event_id = ? AND key = ?",
        values,
    )
    return get_poi_category(conn, event_id, key)


def delete_poi_category(conn: sqlite3.Connection, event_id: int, key: str) -> int:
    """Remove a layer. Refuses while places still belong to it.

    Deleting the layer out from under its POIs would leave them drawn in no
    layer at all - present in the database, invisible on the map, and no error
    anywhere to say so. Returns the count that blocked it, or 0 on success.
    """
    get_poi_category(conn, event_id, key)
    in_use = conn.execute(
        "SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = ?",
        (event_id, key),
    ).fetchone()["c"]
    if in_use:
        return int(in_use)
    conn.execute(
        "DELETE FROM poi_category WHERE event_id = ? AND key = ?", (event_id, key)
    )
    return 0


# --- station roles ----------------------------------------------------------

def seed_roster_roles(conn: sqlite3.Connection, event_id: int) -> None:
    """Give a new event the usual starting set, once.

    Only into an event with no roles at all - the same rule as the place
    layers, and for the same reason. Re-seeding on every read would resurrect a
    role the club deliberately deleted, and a thing that reappears after you
    remove it teaches people not to trust the screen.
    """
    has_any = conn.execute(
        "SELECT 1 FROM roster_role WHERE event_id = ? LIMIT 1", (event_id,)
    ).fetchone()
    if has_any is not None:
        return
    for order, (key, name) in enumerate(DEFAULT_ROSTER_ROLES):
        conn.execute(
            "INSERT OR IGNORE INTO roster_role"
            " (event_id, key, name, sort_order) VALUES (?, ?, ?, ?)",
            (event_id, key, name, order * 10),
        )


def roster_roles(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    """Every role this event has, including ones the club added.

    Filtering to DEFAULT_ROSTER_ROLES here was what made the set closed: a role
    a club added existed in the database and was then dropped on the way out,
    so it could never appear anywhere. Liaison is the obvious missing one - the
    operator embedded with Public Safety is a person on the roster, not just a
    link role.
    """
    seed_roster_roles(conn, event_id)
    return conn.execute(
        "SELECT * FROM roster_role WHERE event_id = ?"
        " ORDER BY sort_order, name",
        (event_id,),
    ).fetchall()


def role_labels(conn: sqlite3.Connection, event_id: int) -> dict[str, str]:
    return {row["key"]: row["name"] for row in roster_roles(conn, event_id)}


def add_roster_role(
    conn: sqlite3.Connection, event_id: int, name: str
) -> sqlite3.Row:
    """Add a role the club needs and the defaults do not have.

    A new role has no status wording of its own in `db.OP_STATUS_LABELS`, so it
    falls back to the generic "Not started / Active / Closed". That is the right
    trade: the alternative was a fixed list, which meant editing Python to
    accept a club that fields a Liaison.
    """
    seed_roster_roles(conn, event_id)
    name = (name or "").strip()
    if not name:
        raise CategoryError("A role needs a name.")
    key = slugify(name)
    if not _KEY_OK.match(key):
        raise CategoryError(f"{name!r} does not make a usable role name.")
    existing = conn.execute(
        "SELECT 1 FROM roster_role WHERE event_id = ? AND key = ?",
        (event_id, key),
    ).fetchone()
    if existing is not None:
        raise CategoryError(f"A role called {name!r} already exists.")
    top = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) AS m FROM roster_role"
        " WHERE event_id = ?", (event_id,),
    ).fetchone()["m"]
    conn.execute(
        "INSERT INTO roster_role (event_id, key, name, sort_order)"
        " VALUES (?, ?, ?, ?)",
        (event_id, key, name, top + 10),
    )
    return conn.execute(
        "SELECT * FROM roster_role WHERE event_id = ? AND key = ?",
        (event_id, key),
    ).fetchone()


def delete_roster_role(conn: sqlite3.Connection, event_id: int, key: str) -> int:
    """Remove a role. Refuses while anyone on the roster holds it.

    Same rule as a place layer: deleting it out from under its people would
    leave them in the database with a role nothing can name, and no error to
    say why. Returns the count that blocked it, or 0 on success.
    """
    seed_roster_roles(conn, event_id)
    in_use = conn.execute(
        "SELECT COUNT(*) AS c FROM roster WHERE event_id = ? AND category = ?",
        (event_id, key),
    ).fetchone()["c"]
    if in_use:
        return int(in_use)
    conn.execute(
        "DELETE FROM roster_role WHERE event_id = ? AND key = ?", (event_id, key))
    return 0


def rename_roster_role(
    conn: sqlite3.Connection, event_id: int, key: str, name: str
) -> sqlite3.Row:
    name = (name or "").strip()
    if not name:
        raise CategoryError("A role needs a name.")
    seed_roster_roles(conn, event_id)
    known = conn.execute(
        "SELECT 1 FROM roster_role WHERE event_id = ? AND key = ?",
        (event_id, key),
    ).fetchone()
    if known is None:
        raise CategoryError(f"Unknown role {key!r}.")
    conn.execute(
        "UPDATE roster_role SET name = ? WHERE event_id = ? AND key = ?",
        (name, event_id, key),
    )
    return conn.execute(
        "SELECT * FROM roster_role WHERE event_id = ? AND key = ?", (event_id, key)
    ).fetchone()
