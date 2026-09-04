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
            courses.append(
                _Course(row["id"], row["name"], coords, geo.cumulative_lengths(coords))
            )
        return cls(courses, max_offset_m)

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

    def locate(self, lat: float, lon: float) -> CoursePosition | None:
        """Nearest point on the nearest course, or None if not near any.

        Where courses share road - which they do for miles - the station is
        reported against whichever line it is closest to. That is a coin flip on
        shared pavement, so the course name is always shown alongside the mile
        figure rather than the mile alone.
        """
        best: CoursePosition | None = None
        for course in self._courses:
            projection = geo.project_onto_line(course.coords, (lon, lat), course.totals)
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
        return best
