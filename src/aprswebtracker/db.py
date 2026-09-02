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
