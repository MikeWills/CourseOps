"""Geometry helpers for course lines.

Coordinates are (lon, lat) pairs throughout, matching GeoJSON and KML order.
That ordering is the opposite of how everyone speaks about positions, so it is
named explicitly in every signature rather than left to be inferred.

Distances use the haversine formula on a spherical Earth. A road course is a few
tens of kilometers, where the spherical error against a proper ellipsoid model is
well under a meter — far below the accuracy of the KML the organizer hands us.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LonLat = tuple[float, float]

EARTH_RADIUS_M = 6371008.8  # IUGG mean radius


def haversine_m(a: LonLat, b: LonLat) -> float:
    """Great-circle distance in meters between two (lon, lat) points."""
    lon1, lat1 = a
    lon2, lat2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def line_length_m(coords: list[LonLat]) -> float:
    """Total length of a polyline in meters."""
    return sum(
        haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)
    )


def reverse(coords: list[LonLat]) -> list[LonLat]:
    """Flip a line drawn finish-to-start."""
    return list(reversed(coords))


def dedupe_consecutive(coords: list[LonLat], tolerance_m: float = 0.5) -> list[LonLat]:
    """Drop repeated points, which KML exports produce at segment joins."""
    if not coords:
        return []
    cleaned = [coords[0]]
    for point in coords[1:]:
        if haversine_m(cleaned[-1], point) > tolerance_m:
            cleaned.append(point)
    return cleaned


def stitch(
    segments: list[list[LonLat]],
    tolerance_m: float = 50.0,
    bridge_m: float = 2_500.0,
) -> tuple[list[LonLat], list[str]]:
    """Join course segments end-to-end into one line.

    Organizer KML routinely splits a course across several LineStrings, in
    arbitrary order and arbitrary direction. This greedily chains segments by
    whichever free end is nearest, reversing them as needed.

    The chain grows at BOTH ends. Growing only from the tail looks sufficient
    until the file happens to list a middle segment first — then the piece that
    belongs at the front gets reversed onto the back instead, and the course
    doubles over itself. That is a silent, plausible-looking corruption, so both
    ends are always considered.

    A segment that cannot be joined within `bridge_m` is LEFT OUT and named in
    the warnings, rather than dragged in across a straight line that was never
    part of the course. A real file made this necessary: an organizer's marathon
    arrived as eight LineStrings, five of which chain perfectly into 26.12 miles
    while the other three are chutes of 0.14, 0.01 and 0.09 miles sitting near
    the start. Pulling those in cost a 5.4 km fabricated leg and reported a
    29.76 mile marathon - a confident, plausible, wrong number of exactly the
    kind this project refuses to print.

    Nothing can reliably tell a spur from a genuinely gapped course, so this
    does not try. It joins what clearly joins, says what it could not place, and
    leaves the judgement to the person looking at the map.

    Returns the joined line and a list of human-readable warnings.
    """
    usable = [dedupe_consecutive(s) for s in segments if len(s) >= 2]
    if not usable:
        return [], ["No segment had two or more points."]

    warnings: list[str] = []
    joined = list(usable.pop(0))

    while usable:
        best = None  # (distance, index, at_front, needs_reverse)

        for index, segment in enumerate(usable):
            candidates = (
                # (distance, at_front, needs_reverse)
                (haversine_m(joined[-1], segment[0]), False, False),
                (haversine_m(joined[-1], segment[-1]), False, True),
                (haversine_m(joined[0], segment[-1]), True, False),
                (haversine_m(joined[0], segment[0]), True, True),
            )
            for distance, at_front, needs_reverse in candidates:
                if best is None or distance < best[0]:
                    best = (distance, index, at_front, needs_reverse)

        distance, index, at_front, needs_reverse = best
        if distance > bridge_m:
            # Nothing left reaches the chain. Joining anyway would invent a
            # kilometres-long straight leg and inflate every mile figure
            # measured along the course afterwards.
            lengths = ", ".join(
                f"{line_length_m(s) / 1609.344:.2f} mi" for s in usable
            )
            warnings.append(
                f"{len(usable)} segment(s) could not be joined and were left "
                f"out ({lengths}) - the nearest was {distance:,.0f} m from the "
                f"course. They are usually start or finish chutes, or belong "
                f"to another route. Check the map, and add them as their own "
                f"course if they are real."
            )
            break


        segment = usable.pop(index)
        if needs_reverse:
            segment = reverse(segment)
        if distance > tolerance_m:
            warnings.append(
                f"Gap of {distance:,.0f} m between segments - check the course "
                f"on the map; the pieces may be out of order or one may belong "
                f"to a different route."
            )
        if at_front:
            joined = segment + joined
        else:
            joined.extend(segment)

    return dedupe_consecutive(joined), warnings


def to_geojson_linestring(coords: list[LonLat]) -> dict:
    return {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in coords]}


def from_geojson_linestring(geometry: dict) -> list[LonLat]:
    return [(float(lon), float(lat)) for lon, lat, *_ in geometry["coordinates"]]


def bounds(coords: list[LonLat]) -> tuple[float, float, float, float] | None:
    """(min_lon, min_lat, max_lon, max_lat), for centering the map on import."""
    if not coords:
        return None
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def centroid(coords: list[LonLat]) -> LonLat | None:
    box = bounds(coords)
    if box is None:
        return None
    min_lon, min_lat, max_lon, max_lat = box
    return ((min_lon + max_lon) / 2, (min_lat + max_lat) / 2)


# --- projecting a station onto a course -------------------------------------
#
# "Full-back is at mile 14.2" is far more actionable over a radio net than a
# lat/lon, and it is the signal that says a road segment is clear: once the
# sweep passes an aid station, that station can tear down and the cones can come
# up. Both NCS and Logistics work off it.


@dataclass(frozen=True)
class Projection:
    """Where a point sits relative to a course line."""

    distance_along_m: float   # travelled from the start of the course
    offset_m: float           # lateral distance from the line
    index: int                # index of the segment it snapped to
    point: LonLat             # the snapped position on the line


def cumulative_lengths(coords: list[LonLat]) -> list[float]:
    """Distance from the start to each vertex. `[0]` is always 0.

    Precomputed once per course: the projection needs it for every station, and
    recomputing 1200 haversines per packet would be wasteful for no gain.
    """
    totals = [0.0]
    for i in range(len(coords) - 1):
        totals.append(totals[-1] + haversine_m(coords[i], coords[i + 1]))
    return totals


def _local_metres(origin: LonLat) -> tuple[float, float]:
    """Metres per degree of longitude and latitude near `origin`.

    An equirectangular approximation. Over the few hundred metres between a
    station and the course it is accurate to well under a metre, and it turns
    the projection into ordinary planar geometry.
    """
    lat_rad = math.radians(origin[1])
    return 111320.0 * math.cos(lat_rad), 110574.0


def project_onto_line(
    coords: list[LonLat],
    target: LonLat,
    totals: list[float] | None = None,
) -> Projection | None:
    """Snap `target` to the nearest point on the polyline.

    Returns None for a degenerate line. Distance along is measured with
    haversine so it agrees with `line_length_m`, while the perpendicular
    projection uses local planar maths.
    """
    if len(coords) < 2:
        return None
    if totals is None:
        totals = cumulative_lengths(coords)

    mx, my = _local_metres(target)
    tx, ty = target[0] * mx, target[1] * my

    best: Projection | None = None
    for i in range(len(coords) - 1):
        ax, ay = coords[i][0] * mx, coords[i][1] * my
        bx, by = coords[i + 1][0] * mx, coords[i + 1][1] * my
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq == 0:
            continue

        # Clamped so the nearest point never runs past either end of the
        # segment - without the clamp a station beside the course would snap to
        # an imaginary extension of the nearest segment.
        t = ((tx - ax) * dx + (ty - ay) * dy) / seg_sq
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)

        px, py = ax + t * dx, ay + t * dy
        offset = math.hypot(tx - px, ty - py)
        if best is not None and offset >= best.offset_m:
            continue

        segment_length = totals[i + 1] - totals[i]
        best = Projection(
            distance_along_m=totals[i] + t * segment_length,
            offset_m=offset,
            index=i,
            point=(px / mx, py / my),
        )

    return best
