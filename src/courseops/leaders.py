"""Lead runner tracking, from aid station reports.

The counterpart to the sweep. The sweep says when an aid station may close; the
leader says when it has to be ready. Between them they bracket the whole field.

**We only learn this when a runner physically passes an operator who reports it
on the net.** There is no tracker on the front runner, so this is a log of
sightings, not a track. Everything else - current position, pace, an estimate
for the next aid station - is derived from those sightings, so nothing can
disagree with the reports the net actually made.

Bib colour matters because it is how an operator identifies which race a runner
is in: "first yellow male just went through" is what gets said, not "first male
in the half marathon".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Divisions a race tracks. Stored as free text in the database so a club can add
# wheelchair or non-binary divisions without a migration; this is just the set
# offered in the UI by default.
DIVISIONS = ("male", "female")
DIVISION_LABELS = {"male": "First male", "female": "First female"}

# A leader who has not been reported for this long is probably between aid
# stations rather than missing - the gap between stations is often several
# miles. Used only to soften the display, never to discard a sighting.
STALE_AFTER_SECONDS = 45 * 60

# Plausible running pace, as metres per second, used to reject a computed pace
# rather than publish an absurd one.
#
# This is not defensive padding: NCS enters these reports as they come over the
# net, and reports arrive in bursts when the net has been busy. Two stations
# logged thirty seconds apart yields a 120 mph "pace" and an ETA to match, and
# an aid station told the leader is two minutes out when they are twenty will
# act on it. When the pace is implausible we show no pace and no estimate,
# which is the honest answer.
#
# 3:00/mile is faster than any human has run a mile; 30:00/mile is slower than
# walking. Anything outside that came from the clock, not the runner.
MIN_PACE_MPS = 1609.344 / (30 * 60)   # 30:00 per mile
MAX_PACE_MPS = 1609.344 / (3 * 60)    # 3:00 per mile


def division_label(division: str) -> str:
    return DIVISION_LABELS.get(division, f"First {division}")


@dataclass(frozen=True)
class Leader:
    """Derived state for one course and division."""

    course_id: int
    course_name: str
    bib_color: str | None
    bib_color_name: str | None
    division: str
    last_poi_id: int | None = None
    last_poi_name: str | None = None
    last_distance_m: float | None = None
    last_at: str | None = None
    last_by: str | None = None
    bib: str | None = None
    pace_mps: float | None = None
    next_poi_id: int | None = None
    next_poi_name: str | None = None
    next_distance_m: float | None = None
    eta_seconds: float | None = None

    @property
    def division_label(self) -> str:
        return division_label(self.division)

    def as_dict(self) -> dict:
        data = {
            "course_id": self.course_id,
            "course_name": self.course_name,
            "bib_color": self.bib_color,
            "bib_color_name": self.bib_color_name,
            "division": self.division,
            "division_label": self.division_label,
            "last_poi_id": self.last_poi_id,
            "last_poi_name": self.last_poi_name,
            "last_distance_m": self.last_distance_m,
            "last_at": self.last_at,
            "last_by": self.last_by,
            "bib": self.bib,
            "pace_mps": self.pace_mps,
            "next_poi_id": self.next_poi_id,
            "next_poi_name": self.next_poi_name,
            "next_distance_m": self.next_distance_m,
            "eta_seconds": self.eta_seconds,
        }
        return data


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def set_bib_color(
    conn: sqlite3.Connection,
    event_id: int,
    course_id: int,
    color: str | None,
    name: str | None = None,
) -> sqlite3.Row:
    """Set the bib colour for a race. Defaults to the course line colour."""
    row = conn.execute(
        "SELECT * FROM course WHERE id = ? AND event_id = ?", (course_id, event_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No course with id {course_id} in this event.")

    from . import styling

    resolved = color or row["color"]
    if resolved is not None and not styling.is_valid_color(resolved):
        raise ValueError(f"{resolved!r} is not a hex colour like #ffcc00.")

    conn.execute(
        "UPDATE course SET bib_color = ?, bib_color_name = ? WHERE id = ?",
        (styling.normalize_color(resolved), (name or "").strip() or None, course_id),
    )
    return conn.execute("SELECT * FROM course WHERE id = ?", (course_id,)).fetchone()


def record_sighting(
    conn: sqlite3.Connection,
    event_id: int,
    course_id: int,
    division: str,
    poi_id: int,
    bib: str | None = None,
    by: str | None = None,
) -> sqlite3.Row:
    """Log that the leader for a division passed an aid station."""
    division = (division or "").strip().lower()
    if not division:
        raise ValueError("A division is required.")

    course = conn.execute(
        "SELECT 1 FROM course WHERE id = ? AND event_id = ?", (course_id, event_id)
    ).fetchone()
    if course is None:
        raise ValueError(f"No course with id {course_id} in this event.")
    poi = conn.execute(
        "SELECT 1 FROM poi WHERE id = ? AND event_id = ?", (poi_id, event_id)
    ).fetchone()
    if poi is None:
        raise ValueError(f"No aid station with id {poi_id} in this event.")

    cur = conn.execute(
        """
        INSERT INTO lead_sighting (event_id, course_id, division, poi_id, bib, by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, course_id, division, poi_id,
         (bib or "").strip()[:16] or None, (by or "").strip()[:24] or None),
    )
    return conn.execute(
        "SELECT * FROM lead_sighting WHERE id = ?", (int(cur.lastrowid),)
    ).fetchone()


def undo_last_sighting(
    conn: sqlite3.Connection, event_id: int, course_id: int, division: str
) -> bool:
    """Remove the most recent sighting. Race day mis-taps happen."""
    row = conn.execute(
        "SELECT id FROM lead_sighting WHERE event_id = ? AND course_id = ?"
        " AND division = ? ORDER BY at DESC, id DESC LIMIT 1",
        (event_id, course_id, division),
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM lead_sighting WHERE id = ?", (row["id"],))
    return True


def sightings(
    conn: sqlite3.Connection, event_id: int, course_id: int, division: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM lead_sighting WHERE event_id = ? AND course_id = ?"
        " AND division = ? ORDER BY at, id",
        (event_id, course_id, division),
    ).fetchall()


def staffed_places(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    """Places where somebody is standing who could see a runner go past.

    Which places those are is a property of the layer, not a magic string. This
    used to be `poi_type == 'aid_station'`, so a club that renamed its layer to
    "Water Stops" lost lead runner tracking silently - the sighting list simply
    went empty, with nothing to say why.
    """
    return conn.execute(
        """
        SELECT p.* FROM poi p
          JOIN poi_category c ON c.event_id = p.event_id AND c.key = p.poi_type
         WHERE p.event_id = ? AND c.staffed = 1
        """,
        (event_id,),
    ).fetchall()


def for_event(
    conn: sqlite3.Connection,
    event_id: int,
    index,
    divisions: tuple[str, ...] = DIVISIONS,
) -> list[Leader]:
    """Current leader state for every course and division.

    `index` is a `progress.CourseIndex`, used to turn each aid station into a
    distance along the course - which is what makes pace and the next station
    computable at all.
    """
    courses = conn.execute(
        "SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id",
        (event_id,),
    ).fetchall()
    poi_rows = staffed_places(conn, event_id)

    # Aid stations in course order, each with its distance along.
    stations = []
    for row in poi_rows:
        located = index.locate(row["lat"], row["lon"])
        if located is not None:
            stations.append((located.distance_along_m, located.course_id, row))
    stations.sort(key=lambda item: item[0])

    results: list[Leader] = []
    for course in courses:
        on_course = [s for s in stations if s[1] == course["id"]]
        for division in divisions:
            results.append(
                _leader_for(conn, event_id, course, division, on_course)
            )
    return results


def _leader_for(conn, event_id, course, division, stations) -> Leader:
    base = dict(
        course_id=course["id"],
        course_name=course["name"],
        bib_color=course["bib_color"] or course["color"],
        bib_color_name=course["bib_color_name"],
        division=division,
    )

    reports = sightings(conn, event_id, course["id"], division)
    distance_by_poi = {row["id"]: distance for distance, _, row in stations}
    name_by_poi = {row["id"]: row["name"] for _, _, row in stations}

    if not reports:
        # Nothing reported yet: the useful thing is which station to expect them
        # at first, so an operator knows what they are waiting for.
        first = stations[0] if stations else None
        return Leader(
            **base,
            next_poi_id=first[2]["id"] if first else None,
            next_poi_name=first[2]["name"] if first else None,
            next_distance_m=first[0] if first else None,
        )

    latest = reports[-1]
    last_distance = distance_by_poi.get(latest["poi_id"])

    # Pace from the previous sighting on this course. Two reports is the minimum
    # for a rate, and using only the last leg keeps it responsive to a runner
    # slowing down late in the race.
    pace = None
    if len(reports) >= 2:
        previous = reports[-2]
        previous_distance = distance_by_poi.get(previous["poi_id"])
        start, end = _parse(previous["at"]), _parse(latest["at"])
        if (previous_distance is not None and last_distance is not None
                and start and end):
            elapsed = (end - start).total_seconds()
            travelled = last_distance - previous_distance
            if elapsed > 0 and travelled > 0:
                candidate = travelled / elapsed
                # Silence beats a confident wrong number: see the note on
                # MIN_PACE_MPS. A burst of catch-up reports must not produce an
                # ETA an aid station would plan around.
                if MIN_PACE_MPS <= candidate <= MAX_PACE_MPS:
                    pace = candidate

    following = None
    if last_distance is not None:
        following = next((s for s in stations if s[0] > last_distance + 1.0), None)

    eta = None
    if following is not None and pace:
        eta = (following[0] - last_distance) / pace

    return Leader(
        **base,
        last_poi_id=latest["poi_id"],
        last_poi_name=name_by_poi.get(latest["poi_id"]),
        last_distance_m=last_distance,
        last_at=latest["at"],
        last_by=latest["by"],
        bib=latest["bib"],
        pace_mps=pace,
        next_poi_id=following[2]["id"] if following else None,
        next_poi_name=following[2]["name"] if following else None,
        next_distance_m=following[0] if following else None,
        eta_seconds=eta,
    )
