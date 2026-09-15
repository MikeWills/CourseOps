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

from . import (access, categories, db, importer, labels, leaders, tracker,
               progress, what3words)


# Loose on purpose: this is a sanity check against a typed label or a pasted
# name, not an attempt to validate every callsign format in the world. Special
# event and foreign calls take shapes a strict pattern would reject.
_CALLSIGN = re.compile(r"^[A-Z0-9]{3,10}(-[A-Z0-9]{1,2})?$")
# A phone designator after tracker.normalise_designator: letters and digits.
# Two characters is OwnTracks' convention ("M1"); the ceiling is the
# endpoint's.
_DESIGNATOR = re.compile(r"^[A-Z0-9]+$")


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
    rows = conn.execute(query + " ORDER BY id DESC", params).fetchall()

    # One GROUP BY per table rather than four COUNTs per event.
    def per_event(sql: str) -> dict[int, int]:
        return {r[0]: r[1] for r in conn.execute(sql).fetchall()}

    counts = {
        "courses": per_event(
            "SELECT event_id, COUNT(*) FROM course GROUP BY event_id"),
        "pois": per_event(
            "SELECT event_id, COUNT(*) FROM poi GROUP BY event_id"),
        "roster": per_event(
            "SELECT event_id, COUNT(*) FROM roster GROUP BY event_id"),
        "pending_imports": per_event(
            "SELECT event_id, COUNT(*) FROM import_feature"
            " WHERE status = 'pending' GROUP BY event_id"),
    }
    out = []
    for row in rows:
        entry = dict(row)
        entry["counts"] = {name: table.get(row["id"], 0)
                           for name, table in counts.items()}
        out.append(entry)
    return out


def _text(payload: dict, key: str, limit: int | None = None) -> str | None:
    """A free-text field of a JSON body, whatever type arrived."""
    return db.clean_text(payload.get(key), limit)


def _ids(values: object, what: str) -> list[int]:
    """A list of integer ids from a JSON body, or a complaint.

    A string where a list was expected iterates its characters, and a null
    inside the list is a TypeError from int() - both were tracebacks.
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{what} must be a list.")
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            raise ValueError(f"{value!r} is not a {what[:-1]} id.") from None
    return out


def _zoom(value: object) -> int:
    try:
        zoom = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a zoom level.") from None
    if not 1 <= zoom <= 20:
        raise ValueError("Zoom is a whole number from 1 to 20.")
    return zoom


@db.transactional
def create_event(conn: sqlite3.Connection, payload: dict,
                 organization_id: int | None = None) -> dict[str, Any]:
    slug = (_text(payload, "slug") or "").lower()
    name = _text(payload, "name") or ""
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

    # The centre is served to every phone as the map's first view, so a
    # string or an object here breaks `map.setView` for every volunteer with
    # no server error - checked the same way a place's coordinates are.
    center = {}
    for axis, column in (("center_lat", "latitude"), ("center_lon", "longitude")):
        value = payload.get(axis)
        if value is not None and value != "":
            center[axis] = _coordinate(value, column)

    event_id = db.create_event(
        conn, slug, name,
        organization_id=organization_id,
        event_date=_text(payload, "event_date"),
        timezone=_text(payload, "timezone") or "UTC",
        center_lat=center.get("center_lat"),
        center_lon=center.get("center_lon"),
    )
    # Role links exist from the moment the event does, so there is never a state
    # where an event has been made but cannot be opened.
    access.ensure_tokens(conn, event_id)
    return dict(db.get_event(conn, slug))


def update_event(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    fields, values = [], []
    if "name" in payload:
        # NOT NULL in the schema, and the form's `required` lets a single
        # space through - which used to reach the column as NULL and 500.
        name = _text(payload, "name")
        if not name:
            raise ValueError("An event needs a name.")
        fields.append("name = ?")
        values.append(name)
    if "event_date" in payload:
        fields.append("event_date = ?")
        values.append(_text(payload, "event_date"))
    if "timezone" in payload:
        timezone = _text(payload, "timezone")
        if not timezone:
            raise ValueError("An event needs a time zone, such as America/Chicago.")
        fields.append("timezone = ?")
        values.append(timezone)
    # Served to every phone as the map's first view: a string here breaks
    # `map.setView` for every volunteer with no server error.
    for axis, column in (("center_lat", "latitude"), ("center_lon", "longitude")):
        if axis in payload and payload[axis] is not None:
            fields.append(f"{axis} = ?")
            values.append(_coordinate(payload[axis], column))
    if "zoom" in payload and payload["zoom"] is not None:
        fields.append("zoom = ?")
        values.append(_zoom(payload["zoom"]))
    if not fields:
        raise ValueError("Nothing to change.")
    values.append(event_id)
    cur = conn.execute(f"UPDATE event SET {', '.join(fields)} WHERE id = ?", values)
    if cur.rowcount == 0:
        raise ValueError("No such event.")
    return dict(conn.execute(
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
        entry = dict(row)
        entry["geojson"] = json.loads(row["geojson"])
        out.append(entry)
    return out


@db.transactional
def assign_features(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    kind = _text(payload, "kind") or ""
    ids = _ids(payload.get("ids", []), "features")
    if not ids:
        raise ValueError("Select at least one feature.")

    if kind == "course":
        name = _text(payload, "name")
        if not name:
            raise ValueError("A course needs a name.")
        course_id, distance_m, warnings = importer.assign_course(
            conn, event_id, ids, name,
            color=_text(payload, "color"),
            reverse=bool(payload.get("reverse")),
            dash=_text(payload, "dash"),
        )
        return {"course_id": course_id, "distance_m": distance_m,
                "warnings": warnings}

    if kind == "poi":
        poi_type = _text(payload, "poi_type") or "aid_station"
        created = [
            importer.assign_poi(
                conn, event_id, feature_id, poi_type,
                name=_text(payload, "name") if len(ids) == 1 else None,
                what3words=what3words.normalize(_text(payload, "what3words")),
            )
            for feature_id in ids
        ]
        return {"poi_ids": created}

    if kind == "discard":
        return {"discarded": importer.discard(conn, event_id, ids)}

    raise ValueError(f"Unknown assignment {kind!r}.")


# --- courses and aid stations ----------------------------------------------

def list_courses(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    return [dict(row) for row in importer.courses_for_event(conn, event_id)]


@db.transactional
def update_course(conn: sqlite3.Connection, event_id: int, course_id: int,
                  payload: dict) -> dict:
    if "bib_color" in payload or "bib_color_name" in payload:
        # Only what the payload names. The form sends the pair, but the API
        # is the API: a name on its own must not reset the colour.
        leaders.set_bib_color(
            conn, event_id, course_id,
            _text(payload, "bib_color") if "bib_color" in payload else leaders.KEEP,
            _text(payload, "bib_color_name") if "bib_color_name" in payload
            else leaders.KEEP,
        )
    style_fields: dict[str, Any] = {}
    if "color" in payload:
        style_fields["color"] = _text(payload, "color") or ""
    if "dash" in payload:
        # An empty dash means solid; it must still reach the update.
        style_fields["dash"] = _text(payload, "dash") or "solid"
    if "name" in payload:
        style_fields["name"] = _text(payload, "name")
        if not style_fields["name"]:
            raise ValueError("A course needs a name.")
    if "sort_order" in payload:
        try:
            style_fields["sort_order"] = int(payload["sort_order"])
        except (TypeError, ValueError):
            raise ValueError("The draw order is a whole number.") from None
    if style_fields:
        importer.set_course_style(conn, event_id, course_id, **style_fields)
    row = conn.execute(
        "SELECT * FROM course WHERE id = ? AND event_id = ?", (course_id, event_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No course with id {course_id} in this event.")
    return dict(row)


def _in_use(parts: list[tuple[int, str, str]]) -> str | None:
    """"1 lead runner sighting and 2 posted stations", or None if nothing."""
    phrases = [f"{n} {one if n == 1 else many}" for n, one, many in parts if n]
    if not phrases:
        return None
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]


def delete_course(conn: sqlite3.Connection, event_id: int,
                  course_id: int) -> str | None:
    """Remove a course. Refuses while lead runner sightings reference it.

    `lead_sighting.course_id` cascades, so a bare DELETE took every report
    for the race with it - the same silent loss that deleting a sighted
    leader refuses. Returns what blocks it, worded for the screen, or None
    once deleted.
    """
    sightings = conn.execute(
        "SELECT COUNT(*) AS c FROM lead_sighting"
        " WHERE event_id = ? AND course_id = ?", (event_id, course_id),
    ).fetchone()["c"]
    blocked = _in_use([
        (sightings, "lead runner sighting", "lead runner sightings")])
    if blocked:
        return blocked
    # The staged features this course was stitched from go back to review.
    # The foreign key only NULLs their target, which left them `assigned` to
    # nothing: off the review screen, undiscardable, and the only way to redo
    # a course stitched wrong was to upload the file again.
    conn.execute(
        "UPDATE import_feature SET status = 'pending'"
        " WHERE event_id = ? AND course_id = ?", (event_id, course_id))
    conn.execute("DELETE FROM course WHERE id = ? AND event_id = ?",
                 (course_id, event_id))
    return None


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
    races: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT poi_id, course_id FROM poi_course WHERE event_id = ?",
        (event_id,),
    ).fetchall():
        races.setdefault(row["poi_id"], []).append(row["course_id"])
    out = []
    for row in index.order_along_course(rows):
        entry = dict(row)
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
        # Which races this place serves. Empty means "not stated", and the
        # lead runner panel falls back to the course it snapped to.
        entry["course_ids"] = sorted(races.get(row["id"], []))
        out.append(entry)
    return out


def _coordinate(value, axis: str) -> float:
    """A lat or lon a human typed, or a clear complaint about it.

    These arrive pasted from a phone or a mapping site, so the failures worth
    naming are a blank field, a stray degree sign, and the two swapped - which
    for a Minnesota event means a longitude of 44 and a latitude of -93, both
    individually valid numbers landing the pin in the Indian Ocean. The range
    check catches the swap for anywhere outside the tropics; nothing can catch
    it inside them, which is why the map picker is the real answer and this is
    the half-day version of it.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"A place needs a {axis}.")
    try:
        number = float(str(value).strip().rstrip("\u00b0"))
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a {axis}.") from None
    limit = 90.0 if axis == "latitude" else 180.0
    if not -limit <= number <= limit:
        raise ValueError(
            f"A {axis} has to be between -{limit:g} and {limit:g}. "
            "Latitude first, then longitude - it is easy to paste them the "
            "other way round."
        )
    return number


@db.transactional
def create_poi(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    """Put a place on the map by hand.

    Not every organizer supplies one. A 5K, a parade, or a vehicle race has
    people standing at points with no "stops" to import, and this is how those
    get onto the map at all.

    This does not contradict "import may never create places": that rule stops
    a FILE filing a parking lot as an aid station without anyone looking. A
    person naming a place and giving it a position IS the human decision the
    rule exists to require.

    `sort_order` is left at 0, which sorts LAST - a new place lands at the end
    of the running order where it is visible, rather than in the middle of a
    sequence the club arranged.
    """
    name = _text(payload, "name")
    if not name:
        raise ValueError("A place needs a name.")

    key = _text(payload, "poi_type")
    if not key:
        raise ValueError("A place needs a layer.")
    categories.get_poi_category(conn, event_id, key)

    lat = _coordinate(payload.get("lat"), "latitude")
    lon = _coordinate(payload.get("lon"), "longitude")

    cur = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (event_id, name, key, lat, lon),
    )
    poi_id = int(cur.lastrowid)

    # The rest of the fields go through the ordinary update, so a place created
    # here and a place edited later are validated by the same code.
    rest = {k: payload[k] for k in
            ("what3words", "label", "notes", "course_ids") if k in payload}
    if rest:
        return update_poi(conn, event_id, poi_id, rest)
    return dict(conn.execute(
        "SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone())


@db.transactional
def update_poi(conn: sqlite3.Connection, event_id: int, poi_id: int,
               payload: dict) -> dict:
    # Checked first, not left to the UPDATE's rowcount: a payload carrying
    # only `course_ids` never reaches that UPDATE, and used to write
    # poi_course rows for - and hand back the name and coordinates of - a
    # place in another club's event.
    current = conn.execute(
        "SELECT * FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id)
    ).fetchone()
    if current is None:
        raise ValueError(f"No aid station with id {poi_id} in this event.")

    fields, values = [], []
    if "name" in payload:
        name = _text(payload, "name")
        if not name:
            raise ValueError("An aid station needs a name.")
        fields.append("name = ?")
        values.append(name)
    if "poi_type" in payload:
        # Must be a layer that exists. An unknown key would leave the place
        # drawn in no layer until something re-seeded one for it, which is a
        # confusing way to lose a pin off the map.
        key = _text(payload, "poi_type") or "aid_station"
        categories.get_poi_category(conn, event_id, key)
        fields.append("poi_type = ?")
        values.append(key)
    if "what3words" in payload:
        words = _text(payload, "what3words")
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
        override = labels.clean(_text(payload, "label"))
        if override and override == labels.derive(
            _text(payload, "name") or current["name"]
        ):
            override = None
        fields.append("label = ?")
        values.append(override)
    if "notes" in payload:
        fields.append("notes = ?")
        values.append(_text(payload, "notes", 2000))
    # Coordinates were import-only, and permanently so: a place dropped in the
    # wrong spot by a hand-drawn organizer file could be renamed, restyled and
    # moved between layers, but never actually moved. Everything downstream
    # reads position - the mile figure, the snap to a course, the pin - so a
    # wrong one is wrong in several places at once and could only be fixed by
    # re-importing the file that was wrong to begin with.
    for axis, column in (("lat", "latitude"), ("lon", "longitude")):
        if axis in payload:
            fields.append(f"{axis} = ?")
            values.append(_coordinate(payload.get(axis), column))

    # Races are a separate table, so they are applied here rather than through
    # the UPDATE below - and a place may legitimately change nothing else.
    changed_races = False
    if "course_ids" in payload:
        set_poi_courses(conn, event_id, poi_id, payload.get("course_ids"))
        changed_races = True

    if not fields:
        if changed_races:
            return dict(conn.execute(
                "SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone())
        raise ValueError("Nothing to change.")

    values.extend([poi_id, event_id])
    cur = conn.execute(
        f"UPDATE poi SET {', '.join(fields)} WHERE id = ? AND event_id = ?", values
    )
    if cur.rowcount == 0:
        raise ValueError(f"No aid station with id {poi_id} in this event.")
    return dict(conn.execute("SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone())


def set_poi_courses(conn: sqlite3.Connection, event_id: int, poi_id: int,
                    course_ids) -> list[int]:
    """Say which races a place serves. Replaces whatever was there.

    Accepts a list, or the comma-separated string the form sends. An empty
    value clears the assignment, which puts the place back on the proximity
    fallback rather than hiding it - "not stated" has to stay expressible,
    because it is what every existing event has.
    """
    if isinstance(course_ids, str):
        parts = [p for p in course_ids.split(",") if p.strip()]
    else:
        parts = list(course_ids or [])
    ids = []
    for part in parts:
        try:
            ids.append(int(part))
        except (TypeError, ValueError):
            raise ValueError(f"{part!r} is not a course id.")

    known = {
        row["id"] for row in conn.execute(
            "SELECT id FROM course WHERE event_id = ?", (event_id,)
        ).fetchall()
    }
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"No course with id {unknown[0]} in this event.")

    conn.execute(
        "DELETE FROM poi_course WHERE event_id = ? AND poi_id = ?",
        (event_id, poi_id),
    )
    for course_id in sorted(set(ids)):
        conn.execute(
            "INSERT INTO poi_course (event_id, poi_id, course_id)"
            " VALUES (?, ?, ?)",
            (event_id, poi_id, course_id),
        )
    return sorted(set(ids))


def reorder_pois(conn: sqlite3.Connection, event_id: int,
                 poi_ids: list[int]) -> int:
    """Set the club's running order for places, in the order given.

    Numbered in tens so a later insertion has somewhere to go without
    renumbering, and 1-based so nothing lands on 0, which means "not placed".

    Only the ids passed are numbered. Anything omitted keeps its 0 and stays
    at the end - which is what should happen to a place the club has not
    thought about yet. `db.reorder` is the one implementation.
    """
    try:
        return db.reorder(conn, "poi", "id", event_id,
                          _ids(poi_ids or [], "places"), allow_subset=True)
    except ValueError as exc:
        raise ValueError(str(exc).replace(
            "Not in this event", "No such place in this event")) from None


def reorder_courses(conn: sqlite3.Connection, event_id: int,
                    course_ids: list[int]) -> int:
    """Set the draw order from a list given TOP FIRST.

    Ascending sort_order is draw order, so the highest draws on top where
    routes share road. The setup table reads as a stack - the first row is
    the one on top - so the first id gets the highest number. Every course
    must be listed: a course left out would keep its old number and land
    somewhere in the stack nobody chose.
    """
    try:
        return db.reorder(conn, "course", "id", event_id,
                          _ids(course_ids or [], "courses"), top_first=True)
    except ValueError:
        raise ValueError(
            "Every course in the event must be listed, once.") from None


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
    ids = _ids(poi_ids or [], "places")
    if not ids:
        raise ValueError("Select at least one place to move.")

    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE poi SET poi_type = ? WHERE event_id = ? AND id IN ({placeholders})",
        [key, event_id, *ids],
    )
    return int(cur.rowcount)


def delete_poi(conn: sqlite3.Connection, event_id: int,
               poi_id: int) -> str | None:
    """Remove a place. Refuses while sightings or a posted operator reference it.

    `lead_sighting.poi_id` cascades and `roster.poi_id` nulls, so a bare
    DELETE took every lead runner report at the place with it - the leader's
    position on the NCS panel jumped back a station - and un-posted whoever
    was standing there, which for an operator who never beacons is the only
    thing putting them on the map. Neither said why. Every other delete in
    this family (a layer with places, a leader with sightings) refuses with
    the count; this one does the same. Setup is used mid-event.

    An incident reported "at" the place keeps its own coordinates and is
    left alone. Returns what blocks the delete, worded for the screen, or
    None once deleted.
    """
    sightings = conn.execute(
        "SELECT COUNT(*) AS c FROM lead_sighting WHERE event_id = ? AND poi_id = ?",
        (event_id, poi_id),
    ).fetchone()["c"]
    posted = conn.execute(
        "SELECT COUNT(*) AS c FROM roster WHERE event_id = ? AND poi_id = ?",
        (event_id, poi_id),
    ).fetchone()["c"]
    blocked = _in_use([
        (sightings, "lead runner sighting", "lead runner sightings"),
        (posted, "posted station", "posted stations"),
    ])
    if blocked:
        return blocked
    # Same as delete_course: the point it came from returns to review.
    conn.execute(
        "UPDATE import_feature SET status = 'pending'"
        " WHERE event_id = ? AND poi_id = ?", (event_id, poi_id))
    conn.execute("DELETE FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id))
    return None


# --- roster -----------------------------------------------------------------

def list_roster(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    pois = {row["id"]: row["name"] for row in conn.execute(
        "SELECT id, name FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()}
    out = []
    for row in db.roster_for_event(conn, event_id):
        entry = dict(row)
        entry["poi_name"] = pois.get(row["poi_id"])
        out.append(entry)
    return out


@db.transactional
def save_roster_entry(conn: sqlite3.Connection, event_id: int, payload: dict) -> dict:
    tracked_by = _text(payload, "tracked_by") or db.TRACKED_BY_APRS
    if tracked_by not in db.TRACKED_BY:
        raise ValueError(f"{tracked_by} is not a way of tracking someone.")
    station_key = (_text(payload, "station_key") or "").upper()
    label = _text(payload, "display_label", 80) or ""
    if tracked_by == db.TRACKED_BY_PHONE:
        # A designator, not a callsign: what the person types into the
        # tracking app. Stored in the same normalised spelling the endpoint
        # matches on, so "m-1" here and "M 1" on the phone are one person.
        station_key = tracker.normalise_designator(station_key)
        if station_key and not _DESIGNATOR.match(station_key):
            raise ValueError(
                f"{station_key} is not usable as a designator. Letters and "
                "digits only - M1, BIKE2 - and short enough to say on the air."
            )
    if not station_key or not label:
        raise ValueError("A roster entry needs a callsign and a label.")
    if tracked_by == db.TRACKED_BY_APRS and not _CALLSIGN.match(station_key):
        raise ValueError(
            f"{station_key} does not look like a callsign. Use the callsign "
            "alone, such as N0CALL, or with its SSID, such as N0CALL-9. For "
            "someone tracked by a phone app, choose Phone app instead."
        )
    # A bare callsign is deliberately allowed, and is the better answer.
    # Volunteers know their callsign; the SSID belongs to whichever radio or
    # phone app they bring on the day, and a coordinator collecting SSIDs weeks
    # in advance collects some wrong ones. The filter is a wildcard per callsign
    # already, so a bare entry is tracked from the first packet that looks like
    # a person - see db.bind_heard_ssid.

    # Editing the callsign here is a RENAME: the human is correcting what
    # they typed. Binding - "the station heard as X is really this person" -
    # is NCS's tool on the live map and goes through change_station_key,
    # which leaves the typed key alone. Sending the edit through the bind
    # logic left two rows for one person: the original, bound to the new key,
    # and a fresh one upserted under it below.
    original = (_text(payload, "original_station_key") or "").upper()
    if original and original != station_key:
        db.rename_station_key(conn, event_id, original, station_key)

    db.upsert_roster_entry(
        conn, event_id, station_key, label,
        category=_text(payload, "category") or "rover",
        expects_aprs=bool(payload.get("expects_aprs", True)),
        operator_name=_text(payload, "operator_name", 80),
        tracked_by=tracked_by,
    )
    if "poi_id" in payload:
        poi_id = payload.get("poi_id") or None
        if poi_id is not None:
            try:
                poi_id = int(poi_id)
            except (TypeError, ValueError):
                raise ValueError(f"{poi_id!r} is not a place id.") from None
        db.assign_station_to_poi(conn, event_id, station_key, poi_id)
    return dict(conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key),
    ).fetchone())


def delete_roster_entry(conn: sqlite3.Connection, event_id: int,
                        station_key: object) -> None:
    key = (db.clean_text(station_key) or "").upper()
    if not key:
        raise ValueError("Which station?")
    conn.execute("DELETE FROM roster WHERE event_id = ? AND station_key = ?",
                 (event_id, key))


# --- access links -----------------------------------------------------------

def list_links(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    return [
        {**dict(row), "role_label": access.ROLE_LABELS.get(row["role"], row["role"])}
        for row in access.tokens_for_event(conn, event_id)
    ]
