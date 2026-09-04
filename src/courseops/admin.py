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
import re
import sqlite3
from typing import Any

from . import (access, categories, db, geo, importer, labels, leaders,
               progress, styling, what3words)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# Loose on purpose: this is a sanity check against a typed label or a pasted
# name, not an attempt to validate every callsign format in the world. Special
# event and foreign calls take shapes a strict pattern would reject.
_CALLSIGN = re.compile(r"^[A-Z0-9]{3,10}(-[A-Z0-9]{1,2})?$")


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
    # The club's own name for each layer, so a table listing places from
    # several layers says which is which while you rename them.
    layers = {
        row["key"]: row
        for row in categories.poi_categories(conn, event_id)
    }
    index = progress.CourseIndex.for_event(conn, event_id)
    rows = conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()
    out = []
    for row in index.order_along_course(rows):
        entry = _row(row)
        layer = layers.get(row["poi_type"])
        entry["layer_name"] = layer["name"] if layer else row["poi_type"]
        entry["layer_icon"] = layer["icon"] if layer else "pin"
        entry["layer_color"] = layer["color"] if layer else None
        located = index.locate(row["lat"], row["lon"])
        entry["distance_along_m"] = located.distance_along_m if located else None
        # The mile never travels alone. Each place is snapped to whichever
        # course line is nearest, which is a coin flip where routes share
        # pavement - so "4.4 mi" on its own invites exactly the reading that
        # these figures are comparable between rows. They are not.
        entry["course_name"] = located.course_name if located else None
        # What the pin will actually read. Shown in setup so a club can see
        # two places that came out the same character and override one.
        entry["label_text"] = labels.for_poi(row["name"], row["label"])
        entry["label_auto"] = labels.derive(row["name"])
        entry["show_labels"] = bool(layer["show_labels"]) if layer else False
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
        # Must be a layer that exists. An unknown key would leave the place
        # drawn in no layer until something re-seeded one for it, which is a
        # confusing way to lose a pin off the map.
        key = (payload.get("poi_type") or "aid_station").strip()
        categories.get_poi_category(conn, event_id, key)
        fields.append("poi_type = ?")
        values.append(key)
    if "what3words" in payload:
        words = payload.get("what3words")
        if words and not what3words.is_plausible(words):
            raise ValueError(
                f"{words!r} does not look like a What3Words address "
                "(three dot-separated words)."
            )
        fields.append("what3words = ?")
        values.append(what3words.normalize(words))
    if "label" in payload:
        # Stored only when it differs from what we would guess anyway. Keeping
        # an override that agrees with the guess would silently freeze the
        # label: rename "Aid 3" to "Aid 4" and the pin would still read 3.
        override = labels.clean(payload.get("label"))
        if override and override == labels.derive(
            (payload.get("name") or "").strip()
            or conn.execute(
                "SELECT name FROM poi WHERE id = ? AND event_id = ?",
                (poi_id, event_id),
            ).fetchone()["name"]
        ):
            override = None
        fields.append("label = ?")
        values.append(override)
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


def reorder_pois(conn: sqlite3.Connection, event_id: int,
                 poi_ids: list[int]) -> int:
    """Set the club's running order for places, in the order given.

    Numbered in tens so a later insertion has somewhere to go without
    renumbering, and 1-based so nothing lands on 0, which means "not placed".

    Only the ids passed are numbered. Anything omitted keeps its 0 and stays
    at the end - which is what should happen to a place the club has not
    thought about yet.
    """
    ids = [int(i) for i in poi_ids or []]
    if not ids:
        raise ValueError("Nothing to reorder.")
    known = {
        row["id"] for row in conn.execute(
            "SELECT id FROM poi WHERE event_id = ?", (event_id,)
        ).fetchall()
    }
    unknown = [i for i in ids if i not in known]
    if unknown:
        # Refuse rather than silently ordering a subset: a half-applied order
        # is worse than none, because it looks like it worked.
        raise ValueError(f"No such place in this event: {unknown[0]}")

    for position, poi_id in enumerate(ids, start=1):
        conn.execute(
            "UPDATE poi SET sort_order = ? WHERE id = ? AND event_id = ?",
            (position * 10, poi_id, event_id),
        )
    return len(ids)


def move_pois(conn: sqlite3.Connection, event_id: int,
              poi_ids: list[int], key: str) -> int:
    """Move several places into a layer at once.

    Organizer KML is usually one flat list - the real Mankato export has no
    folders at all - so every marker arrives in a single layer and has to be
    sorted afterwards. Doing that one row at a time for thirty points is the
    kind of chore that gets abandoned half-finished, which leaves the map
    lying about what is where.
    """
    categories.get_poi_category(conn, event_id, key)
    ids = [int(i) for i in poi_ids or []]
    if not ids:
        raise ValueError("Select at least one place to move.")

    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE poi SET poi_type = ? WHERE event_id = ? AND id IN ({placeholders})",
        [key, event_id, *ids],
    )
    return int(cur.rowcount)


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
    if not _CALLSIGN.match(station_key):
        raise ValueError(
            f"{station_key} does not look like a callsign. Use the callsign "
            "alone, such as N0CALL, or with its SSID, such as N0CALL-9."
        )
    # A bare callsign is deliberately allowed, and is the better answer.
    # Volunteers know their callsign; the SSID belongs to whichever radio or
    # phone app they bring on the day, and a coordinator collecting SSIDs weeks
    # in advance collects some wrong ones. The filter is a wildcard per callsign
    # already, so a bare entry is tracked from the first packet that looks like
    # a person - see db.bind_heard_ssid.

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
