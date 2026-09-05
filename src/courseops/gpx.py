"""GPX course files, read into the same features KML produces.

Consumer route tools - MapMyRun, Strava, Garmin Connect, Ride with GPS - export
GPX, and some offer nothing else. The first event's MapMyRun route arrived that
way. So GPX changes only how features are READ; everything downstream - the
staging table, review, assignment, stitching - is the KML path unchanged.

What GPX gives us:

    <trk> / <trkseg>   a recorded or drawn route; each segment is one line
    <rte>              a planned route; one line
    <wpt>              a point: candidate aid station, start, finish

GPX has no folders, so `folder` is empty and `suggest()` loses that clue.
More features come back unassigned than from a well-organised KML, which is
the correct conservative outcome - a human is assigning them anyway.

**Coordinates are ATTRIBUTES, written lat-first.** `<trkpt lat=".." lon="..">`
is the reverse of KML's `lon,lat` text, and this file is the easiest place in
the project to introduce a silent transposition. Everything leaves here as
(lon, lat), the order geo.py and GeoJSON use; there is a test for it.

`<ele>` (elevation) and `<time>` are ignored. Altitude is not supported for
courses and half-supporting it would be worse than none; time belongs to the
recording, not the route.
"""

from __future__ import annotations

from xml.etree import ElementTree

from .geo import LonLat
from .kml import KmlFeature, KmlError, _local, _text

# A drawn route carries dozens of points per mile; a RECORDED one carries a
# fix every second or two, and a marathon recording runs to tens of thousands.
# That is heavy for the map and for the nearest-point-on-course computation,
# and a recording also carries GPS noise, pauses, and a doubled-back section
# near the start. Nothing is discarded here - simplification is a separate
# decision, and import must never silently lose fidelity - but the review
# screen should say so.
DENSE_TRACK_POINTS = 2000


def _coord(element: ElementTree.Element) -> tuple[LonLat | None, str | None]:
    """(lon, lat) from a point element's lat/lon attributes, or a warning."""
    try:
        lat = float(element.get("lat", ""))
        lon = float(element.get("lon", ""))
    except ValueError:
        return None, "Skipped a point with an unparseable lat/lon"
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None, (
            f"Point lat={lat} lon={lon} is out of range; "
            "this file may have them reversed."
        )
    return (lon, lat), None


def _points(container: ElementTree.Element, tag: str) -> tuple[list[LonLat], list[str]]:
    coords: list[LonLat] = []
    warnings: list[str] = []
    skipped = 0
    for child in container:
        if _local(child.tag) != tag:
            continue
        coord, warning = _coord(child)
        if coord is None:
            skipped += 1
            if warning and warning not in warnings:
                warnings.append(warning)
            continue
        coords.append(coord)
    if skipped > 1:
        warnings.append(f"Skipped {skipped} points that could not be read.")
    return coords, warnings


def _description(element: ElementTree.Element) -> str | None:
    # <desc> is the description; <cmt> is a comment meant for the GPS unit.
    # Either may carry the only free text there is.
    return _text(element, "desc") or _text(element, "cmt")


def _line(name: str, coords: list[LonLat], warnings: list[str],
          description: str | None, kind: str) -> KmlFeature | None:
    if not coords:
        return None
    geom_type = "linestring"
    if len(coords) < 2:
        warnings.append(f"{kind} had fewer than two points; treated as a point.")
        geom_type = "point"
    elif len(coords) > DENSE_TRACK_POINTS:
        warnings.append(
            f"{len(coords)} points - this looks like a recording rather than a "
            "drawn route. It will draw and measure correctly but the map will "
            "be slower; ask the organizer for the planned route if there is one."
        )
    return KmlFeature(
        name=name, folder="", geom_type=geom_type, coords=coords,
        description=description, warnings=warnings,
    )


def features_from_root(root: ElementTree.Element) -> list[KmlFeature]:
    """Every track segment, route and waypoint in a parsed <gpx> document."""
    if _local(root.tag) != "gpx":
        raise KmlError("Not a GPX file: the root element is not <gpx>.")

    features: list[KmlFeature] = []
    for child in root:
        tag = _local(child.tag)
        if tag == "wpt":
            coord, warning = _coord(child)
            if coord is None:
                continue
            features.append(KmlFeature(
                name=_text(child, "name") or "", folder="", geom_type="point",
                coords=[coord], description=_description(child),
                warnings=[warning] if warning else [],
            ))
        elif tag == "rte":
            coords, warnings = _points(child, "rtept")
            feature = _line(_text(child, "name") or "", coords, warnings,
                            _description(child), "Route")
            if feature:
                features.append(feature)
        elif tag == "trk":
            name = _text(child, "name") or ""
            description = _description(child)
            segments = [s for s in child if _local(s.tag) == "trkseg"]
            for index, segment in enumerate(segments):
                coords, warnings = _points(segment, "trkpt")
                # One logical route split into segments. Staged separately and
                # numbered, exactly as a KML MultiGeometry is, so the existing
                # stitching joins them - including a segment drawn backwards.
                suffix = f" [{index + 1}]" if len(segments) > 1 else ""
                feature = _line(f"{name}{suffix}", coords, warnings,
                                description, "Track")
                if feature:
                    features.append(feature)
    return features
