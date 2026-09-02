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
    segments: list[list[LonLat]], tolerance_m: float = 50.0
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

    Returns the joined line and a list of human-readable warnings. Warnings are
    returned rather than raised because a gap is usually still worth importing
    and reviewing on the map — the operator can see whether it matters.
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
