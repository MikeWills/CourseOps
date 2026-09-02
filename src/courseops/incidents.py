"""Runner pickups and other incidents NCS is tracking.

The status workflow is the whole point. A pin that is only "there" or "gone"
does not survive a real event: what NCS needs to see is that a pickup was
requested eight minutes ago and nobody has been dispatched yet. So every
incident carries a status, the time that status began, and a log of who changed
what.

**Medical detail stays out.** Bib, location, status and a short operational note
("unable to continue, waiting at mile 9") are enough to run the net. Recording
a runner's condition would make this a system holding health information about
identifiable people. The bib is the organizer's identifier; we never hold the
bib-to-name mapping.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Two different things end up as a pin on the map, and conflating them was a
# mistake worth naming.
#
# A PICKUP is a dispatch problem: someone is waiting and the question is whether
# anyone is coming. It has a workflow, it is sorted by how long it has gone
# unanswered, and it is finished when the runner has been delivered.
#
# A NOTE is a record: a blocked intersection, a confusing turn, a marshal who
# never arrived. Nobody is being dispatched, so it must never sit in the pickup
# queue looking undispatched - the queue is read as "who is still waiting", and
# a note in it makes that count a lie. Its value is after the event, when the
# organizer wants to know what went wrong at that corner.
KIND_PICKUP = "pickup"
KIND_NOTE = "note"
KINDS = (KIND_PICKUP, KIND_NOTE)

KIND_LABELS = {
    KIND_PICKUP: "Pickup",
    KIND_NOTE: "Course note",
}

# Ordered from first report to done. The order matters: it is the workflow, and
# `next_status` walks it.
#
# "Dropped off" is a distinct step from "closed" because they answer different
# questions. Picked up means the runner is in the vehicle and still SAG's
# responsibility; dropped off means they have been delivered. Closed means the
# incident is off the board, which also covers a request that ended without a
# pickup - the runner decided to carry on, or was collected by someone else.
STATUSES = ("reported", "en_route", "picked_up", "dropped_off", "closed")

STATUS_LABELS = {
    "reported": "Reported",
    "en_route": "En route",
    "picked_up": "Picked up",
    "dropped_off": "Dropped off",
    "closed": "Closed",
}

# What sorts to the top. An unanswered report outranks everything, because the
# failure mode this list exists to prevent is a pickup sitting undispatched.
STATUS_RANK = {"reported": 0, "en_route": 1, "picked_up": 2,
               "dropped_off": 3, "closed": 4}

# Long enough that a note stays operational rather than becoming a narrative.
# See the module docstring: this is a deliberate limit, not an arbitrary one.
MAX_NOTE_LENGTH = 200
MAX_BIB_LENGTH = 16
MAX_WHO_LENGTH = 24


class IncidentError(ValueError):
    """Rejected input. The message is safe to show a user."""


@dataclass(frozen=True)
class Incident:
    row: sqlite3.Row

    def as_dict(self) -> dict:
        data = {key: self.row[key] for key in self.row.keys()}
        data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
        kind = data.get("kind") or KIND_PICKUP
        data["kind"] = kind
        data["kind_label"] = KIND_LABELS.get(kind, kind)
        return data


def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:limit]
    return text or None


def _validate_status(status: str) -> str:
    if status not in STATUSES:
        raise IncidentError(
            f"Unknown status {status!r}. Use one of {', '.join(STATUSES)}."
        )
    return status


def next_status(status: str) -> str | None:
    """The following step in the workflow, or None at the end."""
    index = STATUSES.index(_validate_status(status))
    return STATUSES[index + 1] if index + 1 < len(STATUSES) else None


def create(
    conn: sqlite3.Connection,
    event_id: int,
    lat: float,
    lon: float,
    bib: str | None = None,
    note: str | None = None,
    poi_id: int | None = None,
    by: str | None = None,
    kind: str = KIND_PICKUP,
) -> sqlite3.Row:
    """Open an incident. Bib may be unknown at this point and filled in later."""
    if kind not in KINDS:
        raise IncidentError(
            f"Unknown kind {kind!r}. Use one of {', '.join(KINDS)}."
        )
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise IncidentError(f"Position out of range: {lat}, {lon}")
    if poi_id is not None:
        found = conn.execute(
            "SELECT 1 FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id)
        ).fetchone()
        if found is None:
            raise IncidentError(f"No aid station with id {poi_id} in this event.")

    who = _clean(by, MAX_WHO_LENGTH)
    cur = conn.execute(
        """
        INSERT INTO incident (event_id, kind, bib, lat, lon, poi_id, note,
                              reported_by, status_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, kind, _clean(bib, MAX_BIB_LENGTH), lat, lon, poi_id,
         _clean(note, MAX_NOTE_LENGTH), who, who),
    )
    incident_id = int(cur.lastrowid)
    _log(conn, incident_id, who, "created",
         f"bib {_clean(bib, MAX_BIB_LENGTH) or '(unknown)'}"
         if kind == KIND_PICKUP else "course note")
    return get(conn, event_id, incident_id)


def set_status(
    conn: sqlite3.Connection,
    event_id: int,
    incident_id: int,
    status: str,
    by: str | None = None,
) -> sqlite3.Row:
    """Move an incident along the workflow.

    `status_at` is reset on every change, because it is what "waiting 8 minutes"
    is measured from - the age of the CURRENT state, not of the incident.
    """
    _validate_status(status)
    current = get(conn, event_id, incident_id)
    who = _clean(by, MAX_WHO_LENGTH)

    conn.execute(
        """
        UPDATE incident
           SET status = ?,
               status_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
               status_by = ?,
               closed_at = CASE WHEN ? = 'closed'
                                THEN strftime('%Y-%m-%dT%H:%M:%SZ','now')
                                ELSE NULL END
         WHERE id = ? AND event_id = ?
        """,
        (status, who, status, incident_id, event_id),
    )
    _log(conn, incident_id, who, "status",
         f"{current['status']} -> {status}")
    return get(conn, event_id, incident_id)


def update(
    conn: sqlite3.Connection,
    event_id: int,
    incident_id: int,
    by: str | None = None,
    **fields,
) -> sqlite3.Row:
    """Edit bib, note, assignment or position. Only fields given are touched."""
    get(conn, event_id, incident_id)          # existence check
    allowed = {"bib": MAX_BIB_LENGTH, "note": MAX_NOTE_LENGTH,
               "assigned_to": MAX_WHO_LENGTH}

    updates, values, described = [], [], []
    for name, limit in allowed.items():
        if name in fields:
            value = _clean(fields[name], limit)
            updates.append(f"{name} = ?")
            values.append(value)
            described.append(f"{name}={value or '(cleared)'}")

    if "lat" in fields and "lon" in fields:
        lat, lon = float(fields["lat"]), float(fields["lon"])
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise IncidentError(f"Position out of range: {lat}, {lon}")
        updates += ["lat = ?", "lon = ?"]
        values += [lat, lon]
        described.append("moved")

    if not updates:
        raise IncidentError("Nothing to change.")

    values += [incident_id, event_id]
    conn.execute(
        f"UPDATE incident SET {', '.join(updates)} WHERE id = ? AND event_id = ?",
        values,
    )
    _log(conn, incident_id, _clean(by, MAX_WHO_LENGTH), "edited", ", ".join(described))
    return get(conn, event_id, incident_id)


def get(conn: sqlite3.Connection, event_id: int, incident_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM incident WHERE id = ? AND event_id = ?",
        (incident_id, event_id),
    ).fetchone()
    if row is None:
        raise IncidentError(f"No incident with id {incident_id} in this event.")
    return row


def for_event(
    conn: sqlite3.Connection, event_id: int, include_closed: bool = True
) -> list[sqlite3.Row]:
    """Incidents with the ones needing attention first.

    Pickups before notes, then by status, then by how long they have been
    sitting in it - oldest first. A report nobody has acted on rises to the top
    on its own; a note never rises at all, because nobody is waiting on it.
    """
    query = "SELECT * FROM incident WHERE event_id = ?"
    if not include_closed:
        query += " AND status != 'closed'"
    rows = conn.execute(query, (event_id,)).fetchall()
    return sorted(rows, key=lambda r: (
        0 if (r["kind"] or KIND_PICKUP) == KIND_PICKUP else 1,
        STATUS_RANK.get(r["status"], 9),
        r["status_at"],
    ))


def waiting_count(conn: sqlite3.Connection, event_id: int) -> int:
    """Pickups nobody has finished with - the number that means "still out".

    Notes are excluded by construction: counting them here would turn the one
    number NCS glances at into a number that does not mean anything.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM incident
         WHERE event_id = ? AND kind = ? AND status NOT IN ('dropped_off', 'closed')
        """,
        (event_id, KIND_PICKUP),
    ).fetchone()
    return int(row["c"])


def log_for(conn: sqlite3.Connection, incident_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM incident_log WHERE incident_id = ? ORDER BY at, id",
        (incident_id,),
    ).fetchall()


def _log(
    conn: sqlite3.Connection,
    incident_id: int,
    by: str | None,
    action: str,
    detail: str | None,
) -> None:
    conn.execute(
        "INSERT INTO incident_log (incident_id, by, action, detail)"
        " VALUES (?, ?, ?, ?)",
        (incident_id, by, action, detail),
    )
