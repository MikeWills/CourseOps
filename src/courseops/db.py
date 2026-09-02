"""SQLite access. One file, no server for a club to install."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .parser import PositionReport

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Columns added after a database may already exist in the wild. `CREATE TABLE
# IF NOT EXISTS` silently skips an existing table, so new columns would never
# appear without this. Each entry is applied only if missing, which makes
# init_schema idempotent and safe to run on an old file.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, DDL type/default)
    ("poi", "what3words", "TEXT"),
    ("course", "dash_pattern", "TEXT"),
    ("import_feature", "style_id", "TEXT"),
    ("course", "bib_color", "TEXT"),
    ("course", "bib_color_name", "TEXT"),
    ("roster", "op_status_at", "TEXT"),
    ("roster", "op_status_by", "TEXT"),
]


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Add columns missing from an older database. Returns what was applied."""
    applied = []
    existing_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # freshly created by schema.sql; already has the column
        if column not in _column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    return applied


def init_schema(conn: sqlite3.Connection) -> list[str]:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _apply_migrations(conn)


# --- events ---------------------------------------------------------------

def create_event(conn: sqlite3.Connection, slug: str, name: str, **fields) -> int:
    columns = ["slug", "name", *fields]
    placeholders = ", ".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO event ({', '.join(columns)}) VALUES ({placeholders})",
        [slug, name, *fields.values()],
    )
    return int(cur.lastrowid)


def get_event(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM event WHERE slug = ?", (slug,)).fetchone()


def active_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM event WHERE is_active = 1 ORDER BY id"
    ).fetchall()


# --- roster ---------------------------------------------------------------

def upsert_roster_entry(
    conn: sqlite3.Connection,
    event_id: int,
    station_key: str,
    display_label: str,
    category: str = "rover",
    expects_aprs: bool = True,
    operator_name: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO roster
            (event_id, station_key, display_label, category, expects_aprs, operator_name)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (event_id, station_key) DO UPDATE SET
            display_label = excluded.display_label,
            category      = excluded.category,
            expects_aprs  = excluded.expects_aprs,
            operator_name = excluded.operator_name
        """,
        (event_id, station_key.upper(), display_label, category,
         int(expects_aprs), operator_name),
    )


def roster_for_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label",
        (event_id,),
    ).fetchall()


def tracked_station_keys(conn: sqlite3.Connection, event_id: int) -> list[str]:
    """Roster entries we actually expect packets from — these build the filter."""
    rows = conn.execute(
        "SELECT station_key FROM roster WHERE event_id = ? AND expects_aprs = 1"
        " ORDER BY station_key",
        (event_id,),
    ).fetchall()
    return [r["station_key"] for r in rows]


# --- packets --------------------------------------------------------------

def insert_position(conn: sqlite3.Connection, event_id: int, report: PositionReport) -> int:
    cur = conn.execute(
        """
        INSERT INTO position (
            event_id, station_key, received_at, lat, lon, course_deg, speed_kmh,
            altitude_m, symbol_table, symbol_code, comment, aprs_format, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, report.station_key, report.received_at, report.lat, report.lon,
            report.course_deg, report.speed_kmh, report.altitude_m,
            report.symbol_table, report.symbol_code, report.comment,
            report.aprs_format, report.raw,
        ),
    )
    return int(cur.lastrowid)


def log_raw_packet(
    conn: sqlite3.Connection,
    event_id: int | None,
    received_at: str,
    raw: str,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO raw_packet (event_id, received_at, raw, status, error)"
        " VALUES (?, ?, ?, ?, ?)",
        (event_id, received_at, raw, status, error),
    )


def recent_positions(
    conn: sqlite3.Connection, event_id: int, limit: int = 20
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM position WHERE event_id = ? ORDER BY received_at DESC, id DESC"
        " LIMIT ?",
        (event_id, limit),
    ).fetchall()


def latest_position_per_station(
    conn: sqlite3.Connection, event_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.*
        FROM position p
        JOIN (
            SELECT station_key, MAX(id) AS max_id
            FROM position WHERE event_id = ? GROUP BY station_key
        ) newest ON newest.max_id = p.id
        ORDER BY p.received_at DESC
        """,
        (event_id,),
    ).fetchall()


# Operational status. Manual and NCS-set, deliberately separate from the radio
# status derived from the feed - see the note in schema.sql.
OP_STATUSES = ("pending", "active", "closed")

# Category-specific wording for the same three states. "Torn down" and
# "Finished" mean the same thing operationally but reading the wrong one on a
# radio net costs a clarifying exchange.
OP_STATUS_LABELS = {
    "aid_station": {"pending": "Not staffed", "active": "On station", "closed": "Torn down"},
    "sweep":       {"pending": "Not started", "active": "Rolling",    "closed": "Finished"},
    "sag":         {"pending": "Not started", "active": "Rolling",    "closed": "Finished"},
    "rover":       {"pending": "Not started", "active": "Rolling",    "closed": "Finished"},
    "shadow":      {"pending": "Not started", "active": "Assigned",   "closed": "Released"},
    "net_control": {"pending": "Not open",    "active": "Open",       "closed": "Closed"},
    "start_finish":{"pending": "Not staffed", "active": "Staffed",    "closed": "Closed"},
}
DEFAULT_OP_STATUS_LABELS = {
    "pending": "Not started", "active": "Active", "closed": "Closed",
}


def op_status_label(category: str, op_status: str) -> str:
    return OP_STATUS_LABELS.get(category, DEFAULT_OP_STATUS_LABELS).get(
        op_status, op_status
    )


def set_op_status(
    conn: sqlite3.Connection,
    event_id: int,
    station_key: str,
    op_status: str,
    changed_by: str | None = None,
) -> sqlite3.Row:
    """Set a roster entry's operational status. Raises ValueError if unknown."""
    if op_status not in OP_STATUSES:
        raise ValueError(
            f"Unknown status {op_status!r}. Use one of {', '.join(OP_STATUSES)}."
        )
    # Read the outgoing value first: the roster row is about to be overwritten,
    # and the transition is the useful part at handover.
    previous = conn.execute(
        "SELECT op_status FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key.upper()),
    ).fetchone()

    cur = conn.execute(
        """
        UPDATE roster
           SET op_status = ?,
               op_status_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
               op_status_by = ?
         WHERE event_id = ? AND station_key = ?
        """,
        (op_status, changed_by, event_id, station_key.upper()),
    )
    if cur.rowcount == 0:
        raise ValueError(f"{station_key} is not on this event's roster.")

    # Appended, never overwritten. This history cannot be reconstructed later,
    # so it has to be captured as it happens.
    conn.execute(
        "INSERT INTO roster_status_log"
        " (event_id, station_key, by, from_status, to_status)"
        " VALUES (?, ?, ?, ?, ?)",
        (event_id, station_key.upper(), changed_by,
         previous["op_status"] if previous else None, op_status),
    )
    return conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key.upper()),
    ).fetchone()


def assign_station_to_poi(
    conn: sqlite3.Connection, event_id: int, station_key: str, poi_id: int | None
) -> sqlite3.Row:
    """Post a roster entry at an aid station.

    This is what lets a non-beaconing operator be drawn in the right place and
    sorted into course order - most aid station operators never transmit, so
    their position can only come from the station they are posted at.
    """
    if poi_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id)
        ).fetchone()
        if exists is None:
            raise ValueError(f"No POI with id {poi_id} in this event.")

    cur = conn.execute(
        "UPDATE roster SET poi_id = ? WHERE event_id = ? AND station_key = ?",
        (poi_id, event_id, station_key.upper()),
    )
    if cur.rowcount == 0:
        raise ValueError(f"{station_key} is not on this event's roster.")
    return conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key.upper()),
    ).fetchone()


def excluded_station_keys(conn: sqlite3.Connection, event_id: int) -> set[str]:
    """SSIDs to keep off the map despite their callsign being rostered."""
    return {
        row["station_key"]
        for row in conn.execute(
            "SELECT station_key FROM station_exclusion WHERE event_id = ?",
            (event_id,),
        ).fetchall()
    }


def exclude_station(
    conn: sqlite3.Connection, event_id: int, station_key: str, reason: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO station_exclusion (event_id, station_key, reason)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT (event_id, station_key) DO UPDATE SET reason = excluded.reason",
        (event_id, station_key.strip().upper(), (reason or "").strip() or None),
    )


def unexclude_station(
    conn: sqlite3.Connection, event_id: int, station_key: str
) -> bool:
    cur = conn.execute(
        "DELETE FROM station_exclusion WHERE event_id = ? AND station_key = ?",
        (event_id, station_key.strip().upper()),
    )
    return cur.rowcount > 0


def exclusions(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM station_exclusion WHERE event_id = ? ORDER BY station_key",
        (event_id,),
    ).fetchall()


def rostered_base_callsigns(conn: sqlite3.Connection, event_id: int) -> set[str]:
    """`WX0MIK-1` on the roster means WX0MIK is one of ours, whatever the SSID.

    This is what makes the wildcard filter useful: a packet from WX0MIK-5 is
    kept even though the roster names -1, so a wrong SSID at signup does not
    make someone invisible.
    """
    return {
        key.split("-", 1)[0]
        for key in all_station_keys(conn, event_id)
    }


def unexpected_ssids(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    """SSIDs heard whose callsign is rostered but whose exact SSID is not.

    Derived from what has actually been received, so it needs no separate
    listening step: the app is already ingesting, so it already knows. Surfacing
    this in the UI rather than in a command matters because the failure it
    catches - someone signed up as -1 and beacons -5 - is silent, and a check
    that has to be remembered will be forgotten.

    Excluded SSIDs are left out; they have already been dismissed.
    """
    rostered = set(all_station_keys(conn, event_id))
    bases = {key.split("-", 1)[0] for key in rostered}
    ignored = excluded_station_keys(conn, event_id)
    if not bases:
        return []

    rows = conn.execute(
        """
        SELECT p.station_key,
               COUNT(*)                AS packets,
               MAX(p.received_at)      AS last_at,
               MAX(p.symbol_table)     AS symbol_table,
               MAX(p.symbol_code)      AS symbol_code
          FROM position p
         WHERE p.event_id = ?
      GROUP BY p.station_key
      ORDER BY packets DESC
        """,
        (event_id,),
    ).fetchall()

    out = []
    for row in rows:
        key = row["station_key"]
        if key in rostered or key in ignored:
            continue
        if key.split("-", 1)[0] in bases:
            out.append(row)
    return out


def roster_entries_for_base(
    conn: sqlite3.Connection, event_id: int, base: str
) -> list[sqlite3.Row]:
    """Roster entries sharing a callsign, whatever their SSID."""
    return [
        row for row in roster_for_event(conn, event_id)
        if row["station_key"].split("-", 1)[0] == base.upper()
    ]


def change_station_key(
    conn: sqlite3.Connection, event_id: int, old_key: str, new_key: str
) -> sqlite3.Row:
    """Point a roster entry at the SSID its operator is actually using.

    Keeps the label, category and assignment; only the identity changes. Any
    positions already stored under the new SSID are picked up immediately,
    because the roster is what attributes them.
    """
    old_key, new_key = old_key.strip().upper(), new_key.strip().upper()
    if old_key.split("-", 1)[0] != new_key.split("-", 1)[0]:
        raise ValueError(
            f"{new_key} is a different callsign from {old_key}; "
            "add it as a new roster entry instead."
        )
    existing = conn.execute(
        "SELECT 1 FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, new_key),
    ).fetchone()
    if existing is not None:
        raise ValueError(f"{new_key} is already on the roster.")

    cur = conn.execute(
        "UPDATE roster SET station_key = ? WHERE event_id = ? AND station_key = ?",
        (new_key, event_id, old_key),
    )
    if cur.rowcount == 0:
        raise ValueError(f"{old_key} is not on this event's roster.")
    return conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, new_key),
    ).fetchone()


def op_status_log(
    conn: sqlite3.Connection, event_id: int, station_key: str | None = None
) -> list[sqlite3.Row]:
    """Operational status changes, oldest first.

    Omit `station_key` for the whole event, which is what a shift handover reads.
    """
    query = "SELECT * FROM roster_status_log WHERE event_id = ?"
    params: list = [event_id]
    if station_key is not None:
        query += " AND station_key = ?"
        params.append(station_key.upper())
    return conn.execute(query + " ORDER BY at, id", params).fetchall()


def all_station_keys(conn: sqlite3.Connection, event_id: int) -> list[str]:
    """Every rostered station, beaconing or not.

    Distinct from tracked_station_keys: expects_aprs=0 means "do not alert when
    silent", not "discard if they do report". An aid station operator who turns
    a tracker on mid-event is still one of ours and their position is worth
    keeping.
    """
    rows = conn.execute(
        "SELECT station_key FROM roster WHERE event_id = ? ORDER BY station_key",
        (event_id,),
    ).fetchall()
    return [r["station_key"] for r in rows]
