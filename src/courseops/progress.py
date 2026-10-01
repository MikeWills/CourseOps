"""Where a station is along a course: "Full-back at mile 14.2".

This is the number the net actually speaks. It is also the operational trigger
for the whole event: once the sweep passes an aid station, that station can tear
down and Logistics can pull the cones, so both NCS and the field roles work off
it.

Two honesty constraints shape this module:

1. **A mile figure inherits the course geometry's accuracy.** A hand-drawn route
   that cuts corners with straight-line shortcuts (the Mankato export has 13
   such gaps, one of 1.2 km) is shorter than the road, so the figure drifts. A
   GIS-produced course does not have this. We do not silently smooth it.
2. **A station that is not near any course gets no mile figure at all**, rather
   than a plausible-looking wrong one. Someone will act on this number.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import geo
from .geo import LonLat

# Larger than any sort_order the UI assigns, so "not placed by hand" sorts
# after everything that was.
UNPLACED = 1_000_000_000

# How far off the line a station may be and still be considered "on" a course.
#
# Deliberately generous. GPS is good to tens of metres, but the course line is
# drawn down the middle of the road while a sweep vehicle is on the shoulder,
# and a hand-drawn course can cut a corner by hundreds of metres. Too tight and
# the sweep silently loses its mile marker exactly when it matters; too loose
# and a station on a parallel street reads as on-course. 250 m is a compromise
# that should be revisited against a GIS-produced course.
DEFAULT_MAX_OFFSET_M = 250.0

# The reach for a stop the club TICKED for a race (`poi_course`). A tick is
# the club saying the stop is on that route, so the guard against a stop on a
# neighbouring road does not apply. And a line can sit well off the road by
# design: the Mankato organizer spaced the three races apart so each shows on
# the map, which put water stop A 284 m from the Half. The mile is then only
# as good as the line, which is accepted (see "Mile figures inherit...").
STATED_MAX_OFFSET_M = 500.0


@dataclass(frozen=True)
class CoursePosition:
    course_id: int
    course_name: str
    distance_along_m: float
    remaining_m: float
    course_length_m: float
    offset_m: float

    @property
    def fraction(self) -> float:
        if self.course_length_m <= 0:
            return 0.0
        return min(1.0, self.distance_along_m / self.course_length_m)

    def as_dict(self) -> dict:
        return {
            "course_id": self.course_id,
            "course_name": self.course_name,
            "distance_along_m": self.distance_along_m,
            "remaining_m": self.remaining_m,
            "course_length_m": self.course_length_m,
            "offset_m": self.offset_m,
            "fraction": self.fraction,
        }


@dataclass
class _Course:
    id: int
    name: str
    coords: list[LonLat]
    totals: list[float]
    # The planar geometry, built once with the index: every locate walks
    # every vertex, and rebuilding the segment maths per call was most of
    # the walk.
    line: geo.PlanarLine

    @property
    def length_m(self) -> float:
        return self.totals[-1] if self.totals else 0.0


class CourseIndex:
    """Course geometry, prepared once and reused for every position.

    Cumulative lengths are the expensive part (1200+ haversines for a marathon),
    so they are computed on construction rather than per packet.
    """

    def __init__(self, courses: list[_Course],
                 max_offset_m: float = DEFAULT_MAX_OFFSET_M) -> None:
        self._courses = courses
        self.max_offset_m = max_offset_m
        # (lat, lon) -> the answer, misses included. One snapshot locates
        # every place four times over: in the course-order sort key, for its
        # own course_position, and twice again in the leader progression -
        # and each lookup walks every vertex of every course in pure Python.
        # That was 88 % of a snapshot build, and it grows as places times
        # vertices; the organizer's file has 48 mile markers on a 1258-point
        # course. The index is built per request, so the memo needs no
        # invalidation: it dies with the request.
        self._located: dict[tuple[float, float], CoursePosition | None] = {}
        self._located_on: dict[tuple[float, float, int], CoursePosition | None] = {}

    def __len__(self) -> int:
        return len(self._courses)

    @classmethod
    def for_event(
        cls,
        conn: sqlite3.Connection,
        event_id: int,
        max_offset_m: float = DEFAULT_MAX_OFFSET_M,
    ) -> "CourseIndex":
        courses = []
        for row in conn.execute(
            "SELECT id, name, geojson FROM course WHERE event_id = ? ORDER BY sort_order, id",
            (event_id,),
        ).fetchall():
            coords = geo.from_geojson_linestring(json.loads(row["geojson"]))
            if len(coords) < 2:
                continue
            totals = geo.cumulative_lengths(coords)
            courses.append(_Course(row["id"], row["name"], coords, totals,
                                   geo.PlanarLine(coords, totals)))
        return cls(courses, max_offset_m)

    def area(self, margin_m: float) -> tuple[float, float, float] | None:
        """A circle covering every course plus a margin: (lat, lon, radius_m).

        Centre of the bounding box of all courses, radius to its far corner
        plus the margin. What the APRS-IS radius filter asks for. None when
        the event has no course yet - there is no area to speak of, and the
        buddy filter alone is the right thing until one is imported.
        """
        coords = [c for course in self._courses for c in course.coords]
        box = geo.bounds(coords)
        if box is None:
            return None
        min_lon, min_lat, max_lon, max_lat = box
        centre = ((min_lon + max_lon) / 2, (min_lat + max_lat) / 2)
        half_diagonal = geo.haversine_m(centre, (max_lon, max_lat))
        return centre[1], centre[0], half_diagonal + margin_m

    def order_along_course(self, rows: list) -> list:
        """Sort places into the order they are reached.

        The club's own order wins where it has been set. Everything else falls
        back to distance along the nearest course.

        Sorting by NAME is never right, which is what makes this necessary at
        all: "Aid 10" sorts before "Aid 2", and Greek letters come out Alpha,
        Beta, Delta, Epsilon, Gamma.

        But geometry is not right either once an event has more than one route.
        Each place is snapped to whichever course line is nearest, and where
        routes share pavement that is a coin flip - so a list built from those
        distances interleaves miles measured on three different races. Three
        routes with their own lettered stops is the normal case, and which stop
        follows which is a fact the club holds and the geometry does not. Hence
        `poi.sort_order`, set by dragging the rows in setup.

        `sort_order` 0 means "never placed by hand" and sorts last, by
        distance. So an event nobody has ordered behaves exactly as it always
        did, and a place imported after the ordering was done lands at the end
        where it is visible rather than in the middle where it is not.
        """
        def key(row):
            manual = row["sort_order"] if "sort_order" in row.keys() else 0
            located = self.locate(row["lat"], row["lon"])
            distance = located.distance_along_m if located else float("inf")
            # UNPLACED is beyond any real sort_order, so hand-placed rows lead.
            return (manual or UNPLACED, distance)

        return sorted(rows, key=key)

    def locate_on(self, lat: float, lon: float,
                  course_id: int) -> CoursePosition | None:
        """The nearest point on ONE course, or None if it is not near it.

        What a distance along a particular race needs. `locate` answers
        "which line is this nearest", and on shared road that is a coin flip
        between races whose miles have nothing to do with each other.
        """
        key = (lat, lon, course_id)
        try:
            return self._located_on[key]
        except KeyError:
            pass
        answer = None
        course = next((c for c in self._courses if c.id == course_id), None)
        if course is not None:
            projection = course.line.project((lon, lat))
            if projection is not None and projection.offset_m <= self.max_offset_m:
                answer = CoursePosition(
                    course_id=course.id,
                    course_name=course.name,
                    distance_along_m=projection.distance_along_m,
                    remaining_m=max(0.0, course.length_m - projection.distance_along_m),
                    course_length_m=course.length_m,
                    offset_m=projection.offset_m,
                )
        self._located_on[key] = answer
        return answer

    def _position(self, course: _Course, projection) -> CoursePosition:
        return CoursePosition(
            course_id=course.id,
            course_name=course.name,
            distance_along_m=projection.distance_along_m,
            remaining_m=max(0.0, course.length_m - projection.distance_along_m),
            course_length_m=course.length_m,
            offset_m=projection.offset_m,
        )

    def progression(self, course_id: int, stops: list,
                    stated: set[int] | None = None) -> dict[int, CoursePosition | None]:
        """Each stop's position on ONE race, the stops in the club's order.

        Where the race goes by a stop more than once, which pass is meant
        is chosen for the whole race at once: one pass per stop such that
        the miles never run backwards in the club's order, and of those the
        choice that puts the stops nearest the line in total. Nearest-pass
        alone put the Mankato Full's water stop I at mile 20.6 (54 m off)
        instead of 16.3 (80 m off), behind J at 16.8; "first pass after the
        previous stop" alone took a pass 200 m away over one the stop sits
        on. Together they are right in both cases.

        A stop that fits nowhere in order (an order that runs against the
        route) is left out of the ordering and given its nearest pass, so it
        cannot drag the stops after it. A stop no pass comes near is None.

        `stated` names the stops (poi ids) the club ticked for this race;
        they reach `STATED_MAX_OFFSET_M`, the rest `max_offset_m`.
        """
        stated = stated or set()
        course = next((c for c in self._courses if c.id == course_id), None)
        if course is None:
            return {row["id"]: None for row in stops}
        # A stop beside another on the same spot projects a few metres
        # "before" it; this much backwards is still in order.
        slack = 50.0
        skip = 1e6            # far above any sum of offsets
        options = [course.line.passes(
                       (row["lon"], row["lat"]),
                       max(self.max_offset_m, STATED_MAX_OFFSET_M)
                       if row["id"] in stated else self.max_offset_m)
                   for row in stops]
        placeable = [j for j, found in enumerate(options) if found]

        # best[(j, k)] = (cost, previous (j, k) or None) for stop j at pass k,
        # every placeable stop before it either placed in order or skipped.
        best: dict[tuple[int, int], tuple[float, tuple[int, int] | None]] = {}
        for n, j in enumerate(placeable):
            for k, here in enumerate(options[j]):
                cost, back = skip * n, None           # everything before skipped
                for m, i in enumerate(placeable[:n]):
                    for kk, there in enumerate(options[i]):
                        if there.distance_along_m > here.distance_along_m + slack:
                            continue
                        candidate = best[(i, kk)][0] + skip * (n - m - 1)
                        if candidate < cost:
                            cost, back = candidate, (i, kk)
                best[(j, k)] = (cost + here.offset_m, back)

        chosen: dict[int, int] = {}
        if best:
            last = len(placeable) - 1
            end = min(best, key=lambda key: best[key][0]
                      + skip * (last - placeable.index(key[0])))
            node: tuple[int, int] | None = end
            while node is not None:
                chosen[node[0]] = node[1]
                node = best[node][1]

        out: dict[int, CoursePosition | None] = {}
        for j, row in enumerate(stops):
            if not options[j]:
                out[row["id"]] = None
            elif j in chosen:
                out[row["id"]] = self._position(course, options[j][chosen[j]])
            else:
                nearest = min(options[j], key=lambda p: p.offset_m)
                out[row["id"]] = self._position(course, nearest)
        return out

    def place_positions(self, ordered_rows: list,
                        served: dict[int, set[int]]) -> dict[int, CoursePosition | None]:
        """Every place's mile, as the club reads it.

        On a race it serves - the one highest on the Courses tab - at the
        pass its place in the club's order says. A place serving no stated
        race, or none within reach, keeps the nearest line as before.
        `ordered_rows` must already be in club order (`order_along_course`).
        """
        per_course: dict[int, dict[int, CoursePosition | None]] = {}
        for course in self._courses:
            on_it = [row for row in ordered_rows
                     if course.id in served.get(row["id"], ())]
            if on_it:
                per_course[course.id] = self.progression(
                    course.id, on_it, {row["id"] for row in on_it})
        out: dict[int, CoursePosition | None] = {}
        for row in ordered_rows:
            found = None
            ticked = served.get(row["id"]) or set()
            for course in reversed(self._courses):        # top of the stack first
                if course.id in ticked:
                    found = per_course.get(course.id, {}).get(row["id"])
                    if found is not None:
                        break
            out[row["id"]] = found or self.locate(row["lat"], row["lon"])
        return out

    def locate(self, lat: float, lon: float) -> CoursePosition | None:
        """Nearest point on the nearest course, or None if not near any.

        Where courses share road - which they do for miles - the station is
        reported against whichever line it is closest to. That is a coin flip on
        shared pavement, so the course name is always shown alongside the mile
        figure rather than the mile alone.
        """
        key = (lat, lon)
        try:
            return self._located[key]
        except KeyError:
            pass
        best: CoursePosition | None = None
        for course in self._courses:
            projection = course.line.project((lon, lat))
            if projection is None or projection.offset_m > self.max_offset_m:
                continue
            if best is not None and projection.offset_m >= best.offset_m:
                continue
            best = CoursePosition(
                course_id=course.id,
                course_name=course.name,
                distance_along_m=projection.distance_along_m,
                remaining_m=max(0.0, course.length_m - projection.distance_along_m),
                course_length_m=course.length_m,
                offset_m=projection.offset_m,
            )
        self._located[key] = best
        return best


def served_courses(conn: sqlite3.Connection, event_id: int) -> dict[int, set[int]]:
    """poi id -> the races the club ticked for it (`poi_course`).

    Stated, never guessed. A place with no entry serves nothing stated, and
    `place_positions` measures it on the nearest line as before.
    """
    served: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT poi_id, course_id FROM poi_course WHERE event_id = ?", (event_id,)
    ).fetchall():
        served.setdefault(row["poi_id"], set()).add(row["course_id"])
    return served
