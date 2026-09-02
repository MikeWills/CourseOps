"""Setup API: everything a club needs to configure an event without a terminal.

The project's whole premise is that a radio club can stand this up without much
effort. A CLI-only setup contradicted that - a dozen commands before anyone sees
a map - so every setup operation is available here too.

Two things deliberately stay outside the UI, because they have to happen before
it exists: the callsign in `.env`, and starting the server. Everything after
that is forms.

The CLI is kept, not replaced. It is better for repeat or scripted setup, and it
is how the test suite drives the same code paths.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import access, db, geo, importer, leaders, progress, styling, what3words


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# --- events -----------------------------------------------------------------

def list_events(conn: sqlite3.Connection,
                organization_id: int | None = None) -> list[dict[str, Any]]:
    """Events, optionally limited to one organization.

    Filtering here rather than in the caller means a club never receives another
    club's event names, not even to discard - which would otherwise leak their
    race calendar.
    """
    query = "SELECT * FROM event"
    params: list = []
    if organization_id is not None:
        query += " WHERE organization_id = ?"
        params.append(organization_id)
    out = []
    for row in conn.execute(query + " ORDER BY id DESC", params).fetchall():
        entry = _row(row)
        entry["counts"] = {
            "courses": conn.execute(
                "SELECT COUNT(*) FROM course WHERE event_id = ?", (row["id"],)
            ).fetchone()[0],
            "pois": conn.execute(
                "SELECT COUNT(*) FROM poi WHERE event_id = ?", (row["id"],)
            ).fetchone()[0],
            "roster": conn.execute(
                "SELECT COUNT(*) FROM roster WHERE event_id = ?", (row["id"],)
            ).fetchone()[0],
            "pending_imports": conn.execute(
                "SELECT COUNT(*) FROM import_feature"
                " WHERE event_id = ? AND status = 'pending'", (row["id"],)
            ).fetchone()[0],
        }
        out.append(entry)
    return out


def create_event(conn: sqlite3.Connection, payload: dict,
                 organization_id: int | None = None) -> dict[str, Any]:
    slug = (payload.get("slug") or "").strip().lower()
    name = (payload.get("name") or "").strip()
    if not slug or not name:
        raise ValueError("An event needs a short name (slug) and a full name.")
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            "The short name may only contain letters, numbers, - and _ "
            "(it appears in the link)."
        )
    if db.get_event(conn, slug) is not None:
        raise ValueError(f"An event called {slug!r} already exists.")

    if organization_id is None:
        raise ValueError("An event must belong to an organization.")

    event_id = db.create_event(
        conn, slug, name,
        organization_id=organization_id,
        event_date=(payload.get("event_date") or "").strip() or None,
        timezone=(payload.get("timezone") or "UTC").strip(),
        center_lat=payload.get("center_lat"),
        center_lon=payload.get("center_lon"),
    )
    # Role links exist from the moment the event does, so there is never a state
    # where an event has been made but cannot be opened.
    access.ensure_tokens(conn, event_id)
    return _row(db.get_event(conn, slug))


def update_event(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    fields, values = [], []
    for name in ("name", "event_date", "timezone"):
        if name in payload:
            fields.append(f"{name} = ?")
            values.append((payload.get(name) or "").strip() or None)
    for name in ("center_lat", "center_lon", "zoom"):
        if name in payload and payload[name] is not None:
            fields.append(f"{name} = ?")
            values.append(payload[name])
    if not fields:
        raise ValueError("Nothing to change.")
    values.append(event_id)
    conn.execute(f"UPDATE event SET {', '.join(fields)} WHERE id = ?", values)
    return _row(conn.execute(
        "SELECT * FROM event WHERE id = ?", (event_id,)
    ).fetchone())


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    # Foreign keys cascade, so this removes courses, roster, positions and the
    # whole history with it. The UI asks first.
    conn.execute("DELETE FROM event WHERE id = ?", (event_id,))


# --- course import ----------------------------------------------------------

def staged_features(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    """Pending features, with geometry, so the UI can draw them on a map.

    Showing them is the point. The plan called for a review *screen* precisely
    because organizer files are wrong in ways a list of names cannot reveal - a
    course split in five, a stray line miles away, a folder mixing water stops
    with parking. Seeing them on the map is what makes the decision obvious.
    """
    out = []
    for row in importer.pending_features(conn, event_id):
        entry = _row(row)
        entry["geojson"] = json.loads(row["geojson"])
        out.append(entry)
    return out


def assign_features(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    kind = (payload.get("kind") or "").strip()
    ids = [int(i) for i in payload.get("ids", [])]
    if not ids:
        raise ValueError("Select at least one feature.")

    if kind == "course":
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("A course needs a name.")
        course_id, distance_m, warnings = importer.assign_course(
            conn, event_id, ids, name,
            color=payload.get("color") or None,
            reverse=bool(payload.get("reverse")),
            dash=payload.get("dash") or None,
        )
        return {"course_id": course_id, "distance_m": distance_m,
                "warnings": warnings}

    if kind == "poi":
        poi_type = (payload.get("poi_type") or "aid_station").strip()
        created = [
            importer.assign_poi(
                conn, event_id, feature_id, poi_type,
                name=(payload.get("name") or None) if len(ids) == 1 else None,
                what3words=what3words.normalize(payload.get("what3words")),
            )
            for feature_id in ids
        ]
        return {"poi_ids": created}

    if kind == "discard":
        return {"discarded": importer.discard(conn, ids)}

    raise ValueError(f"Unknown assignment {kind!r}.")


# --- courses and aid stations ----------------------------------------------

def list_courses(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    return [_row(row) for row in importer.courses_for_event(conn, event_id)]


def update_course(conn: sqlite3.Connection, event_id: int, course_id: int,
                  payload: dict) -> dict:
    if "bib_color" in payload or "bib_color_name" in payload:
        leaders.set_bib_color(
            conn, event_id, course_id,
            payload.get("bib_color"), payload.get("bib_color_name"),
        )
    style_fields = {k: payload[k] for k in ("color", "dash", "name", "sort_order")
                    if k in payload}
    if style_fields:
        importer.set_course_style(conn, event_id, course_id, **style_fields)
    return _row(conn.execute(
        "SELECT * FROM course WHERE id = ? AND event_id = ?", (course_id, event_id)
    ).fetchone())


def delete_course(conn: sqlite3.Connection, event_id: int, course_id: int) -> None:
    conn.execute("DELETE FROM course WHERE id = ? AND event_id = ?",
                 (course_id, event_id))


def list_pois(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()
    out = []
    for row in index.order_along_course(rows):
        entry = _row(row)
        located = index.locate(row["lat"], row["lon"])
        entry["distance_along_m"] = located.distance_along_m if located else None
        out.append(entry)
    return out


def update_poi(conn: sqlite3.Connection, event_id: int, poi_id: int,
               payload: dict) -> dict:
    fields, values = [], []
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("An aid station needs a name.")
        fields.append("name = ?")
        values.append(name)
    if "poi_type" in payload:
        fields.append("poi_type = ?")
        values.append((payload.get("poi_type") or "aid_station").strip())
    if "what3words" in payload:
        words = payload.get("what3words")
        if words and not what3words.is_plausible(words):
            raise ValueError(
                f"{words!r} does not look like a What3Words address "
                "(three dot-separated words)."
            )
        fields.append("what3words = ?")
        values.append(what3words.normalize(words))
    if "notes" in payload:
        fields.append("notes = ?")
        values.append((payload.get("notes") or "").strip() or None)
    if not fields:
        raise ValueError("Nothing to change.")

    values.extend([poi_id, event_id])
    cur = conn.execute(
        f"UPDATE poi SET {', '.join(fields)} WHERE id = ? AND event_id = ?", values
    )
    if cur.rowcount == 0:
        raise ValueError(f"No aid station with id {poi_id} in this event.")
    return _row(conn.execute("SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone())


def delete_poi(conn: sqlite3.Connection, event_id: int, poi_id: int) -> None:
    conn.execute("DELETE FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id))


# --- roster -----------------------------------------------------------------

def list_roster(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    pois = {row["id"]: row["name"] for row in conn.execute(
        "SELECT id, name FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()}
    out = []
    for row in db.roster_for_event(conn, event_id):
        entry = _row(row)
        entry["poi_name"] = pois.get(row["poi_id"])
        out.append(entry)
    return out


def save_roster_entry(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    station_key = (payload.get("station_key") or "").strip().upper()
    label = (payload.get("display_label") or "").strip()
    if not station_key or not label:
        raise ValueError("A roster entry needs a callsign and a label.")
    if "-" not in station_key:
        # Not fatal, but almost always a mistake: the SSID is part of the
        # identity, and a bare callsign usually means the home station.
        raise ValueError(
            f"{station_key} has no SSID. Use the full callsign they transmit "
            "with, such as {}-9.".format(station_key)
        )

    original = (payload.get("original_station_key") or "").strip().upper()
    if original and original != station_key:
        db.change_station_key(conn, event_id, original, station_key)

    db.upsert_roster_entry(
        conn, event_id, station_key, label,
        category=(payload.get("category") or "rover").strip(),
        expects_aprs=bool(payload.get("expects_aprs", True)),
        operator_name=(payload.get("operator_name") or "").strip() or None,
    )
    if "poi_id" in payload:
        db.assign_station_to_poi(conn, event_id, station_key,
                                 payload.get("poi_id") or None)
    return _row(conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key),
    ).fetchone())


def delete_roster_entry(conn: sqlite3.Connection, event_id: int,
                        station_key: str) -> None:
    conn.execute("DELETE FROM roster WHERE event_id = ? AND station_key = ?",
                 (event_id, station_key.strip().upper()))


# --- access links -----------------------------------------------------------

def list_links(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    return [
        {**_row(row), "role_label": access.ROLE_LABELS.get(row["role"], row["role"])}
        for row in access.tokens_for_event(conn, event_id)
    ]
