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


def clear_sightings(
    conn: sqlite3.Connection, event_id: int, course_id: int, division: str
) -> int:
    """Throw away every sighting for one race and division. Returns the count.

    Undo removes one report, which is right for a mis-tap during the race and
    useless the morning of it: a club that rehearsed the panel, or ran the same
    event last year on the same database, starts with a leader already halfway
    round. Pressing undo eleven times is not a reset.

    Deliberately scoped to one course and division rather than the whole event,
    so this is the same shape as every other control on that row and cannot
    take out a race that has genuinely started alongside one that has not.
    """
    cur = conn.execute(
        "DELETE FROM lead_sighting WHERE event_id = ? AND course_id = ?"
        " AND division = ?",
        (event_id, course_id, (division or "").strip().lower()),
    )
    return int(cur.rowcount)


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

    # Every staffed place in the event, whatever course it snapped to.
    #
    # NCS can report the leader at ANY station - the picker offers all of them,
    # because which race a stop belongs to is inferred from proximity and that
    # is a coin flip where routes share pavement. So the lookups used to NAME a
    # sighting have to cover everything. Building them from one course's
    # stations meant a correction to a station that snapped elsewhere was
    # stored and then displayed as nothing at all: the report vanished, and the
    # leader appeared stuck where they were.
    # Which races each place serves, as STATED by the club.
    stated: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT poi_id, course_id FROM poi_course WHERE event_id = ?",
        (event_id,),
    ).fetchall():
        stated.setdefault(row["poi_id"], set()).add(row["course_id"])

    known = {}
    for row in poi_rows:
        located = index.locate(row["lat"], row["lon"])
        # Stated beats snapped. A stop can serve several races - the organizer
        # names them "WATER (ALL)" - and snapping picks exactly one, so the
        # progression for a race silently skipped every stop that happened to
        # sit closer to another line: A, B, C, D, I with E to H missing.
        # Nothing stated falls back to the snap, so an event that predates
        # this behaves as it did.
        serves = stated.get(row["id"])
        if not serves:
            serves = {located.course_id} if located else set()
        known[row["id"]] = (
            row["name"],
            located.distance_along_m if located else None,
            serves,
        )

    # Ordered the way the club reads them: their own order where they set one,
    # distance along the course otherwise. This is what "the next station"
    # means, so it has to agree with the list in setup.
    ordered = index.order_along_course(poi_rows)

    results: list[Leader] = []
    for course in courses:
        on_course = [
            row for row in ordered if course["id"] in known[row["id"]][2]
        ]
        for division in divisions:
            results.append(
                _leader_for(conn, event_id, course, division, on_course, known)
            )
    return results


def _leader_for(conn, event_id, course, division, stations, known) -> Leader:
    base = dict(
        course_id=course["id"],
        course_name=course["name"],
        bib_color=course["bib_color"] or course["color"],
        bib_color_name=course["bib_color_name"],
        division=division,
    )

    reports = sightings(conn, event_id, course["id"], division)
    # `known` covers every staffed place in the event, so a sighting recorded
    # at a station that snapped to another course is still named.
    distance_by_poi = {poi_id: d for poi_id, (_, d, _) in known.items()}
    name_by_poi = {poi_id: n for poi_id, (n, _, _) in known.items()}
    positions = [
        (distance_by_poi.get(row["id"]), row) for row in stations
    ]

    if not reports:
        # Nothing reported yet: the useful thing is which station to expect them
        # at first, so an operator knows what they are waiting for.
        first = stations[0] if stations else None
        return Leader(
            **base,
            next_poi_id=first["id"] if first else None,
            next_poi_name=first["name"] if first else None,
            next_distance_m=distance_by_poi.get(first["id"]) if first else None,
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

    # The next station is the one after the last SIGHTING in the club's order,
    # not the next one further down the course. Those differ the moment an
    # order is set by hand, which is the whole reason it exists.
    following = None
    seen = [i for i, (_, row) in enumerate(positions)
            if row["id"] == latest["poi_id"]]
    if seen:
        following = next((p for p in positions[seen[0] + 1:]), None)
    elif last_distance is not None:
        # The sighting was at a station that is not on this course at all - a
        # correction typed against the wrong race, or a stop the snap put
        # elsewhere. Name it, but do not guess what comes next.
        following = None

    eta = None
    if (following is not None and pace
            and following[0] is not None and last_distance is not None):
        gap = following[0] - last_distance
        # A hand-set order can run against the geometry, and a negative gap
        # would produce an ETA in the past. No figure beats a wrong one.
        if gap > 0:
            eta = gap / pace

    return Leader(
        **base,
        last_poi_id=latest["poi_id"],
        last_poi_name=name_by_poi.get(latest["poi_id"]),
        last_distance_m=last_distance,
        last_at=latest["at"],
        last_by=latest["by"],
        bib=latest["bib"],
        pace_mps=pace,
        next_poi_id=following[1]["id"] if following else None,
        next_poi_name=following[1]["name"] if following else None,
        next_distance_m=following[0] if following else None,
        eta_seconds=eta,
    )
