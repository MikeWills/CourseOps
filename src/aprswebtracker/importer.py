"""KML/KMZ import: stage parsed features, then commit reviewed ones.

Import is deliberately two-phase. Phase one parses a file into `import_feature`
rows marked `pending`; phase two assigns each one to a course or a POI. Nothing
reaches `course` or `poi` without a human confirming it.

That split exists because organizer KML is reliably wrong in ways no heuristic
catches: placemarks named "Untitled Path", a course split across five segments in
arbitrary order, a folder mixing water stops with parking and porta-potties. A
silent importer would file those incorrectly and cost more time to untangle than
the review step costs to run.

Import is also additive and repeatable — the full course, the half course and the
water stops usually arrive as three separate files exported from three tools.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import geo, kml
from .geo import LonLat


@dataclass
class ImportSummary:
    batch_id: int
    filename: str
    total: int
    by_type: dict[str, int]
    warnings: list[str]


def stage_file(
    conn: sqlite3.Connection, event_id: int, path: str | Path
) -> ImportSummary:
    """Parse a KML/KMZ and stage its features for review. Raises kml.KmlError."""
    file_path = Path(path)
    features = kml.load(file_path)
    source_kind = "kmz" if zipfile.is_zipfile(file_path) else "kml"

    cur = conn.execute(
        "INSERT INTO import_batch (event_id, filename, source_kind) VALUES (?, ?, ?)",
        (event_id, file_path.name, source_kind),
    )
    batch_id = int(cur.lastrowid)

    by_type: dict[str, int] = {}
    warnings: list[str] = []

    for feature in features:
        geometry = (
            {"type": "Point", "coordinates": list(feature.coords[0])}
            if feature.geom_type == "point"
            else geo.to_geojson_linestring(feature.coords)
        )
        conn.execute(
            """
            INSERT INTO import_feature (
                batch_id, event_id, name, folder, geom_type, geojson,
                length_m, description, warnings, suggestion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id, event_id, feature.label, feature.folder,
                feature.geom_type, json.dumps(geometry), feature.length_m,
                feature.description,
                "\n".join(feature.warnings) or None,
                feature.suggest(),
            ),
        )
        by_type[feature.geom_type] = by_type.get(feature.geom_type, 0) + 1
        warnings.extend(f"{feature.label}: {w}" for w in feature.warnings)

    return ImportSummary(batch_id, file_path.name, len(features), by_type, warnings)


def pending_features(
    conn: sqlite3.Connection, event_id: int, include_all: bool = False
) -> list[sqlite3.Row]:
    query = "SELECT * FROM import_feature WHERE event_id = ?"
    if not include_all:
        query += " AND status = 'pending'"
    return conn.execute(query + " ORDER BY id", (event_id,)).fetchall()


def get_feature(conn: sqlite3.Connection, feature_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM import_feature WHERE id = ?", (feature_id,)
    ).fetchone()


def _coords_of(row: sqlite3.Row) -> list[LonLat]:
    geometry = json.loads(row["geojson"])
    if geometry["type"] == "Point":
        lon, lat, *_ = geometry["coordinates"]
        return [(float(lon), float(lat))]
    return geo.from_geojson_linestring(geometry)


def assign_course(
    conn: sqlite3.Connection,
    event_id: int,
    feature_ids: list[int],
    name: str,
    color: str | None = None,
    reverse: bool = False,
) -> tuple[int, float, list[str]]:
    """Build one course from one or more staged line features.

    Several features are stitched end-to-end, since a course routinely arrives
    split across segments. Returns (course_id, distance_m, warnings).
    """
    rows = [get_feature(conn, fid) for fid in feature_ids]
    missing = [fid for fid, row in zip(feature_ids, rows) if row is None]
    if missing:
        raise ValueError(f"No staged feature with id {missing}")

    wrong_type = [r["id"] for r in rows if r["geom_type"] == "point"]
    if wrong_type:
        raise ValueError(
            f"Feature(s) {wrong_type} are points, not lines; a course needs a line."
        )

    segments = [_coords_of(r) for r in rows]
    if len(segments) == 1:
        coords = geo.dedupe_consecutive(segments[0])
        warnings: list[str] = []
    else:
        coords, warnings = geo.stitch(segments)

    if reverse:
        coords = geo.reverse(coords)
    if len(coords) < 2:
        raise ValueError("Result has fewer than two points; nothing to draw.")

    distance_m = geo.line_length_m(coords)
    cur = conn.execute(
        "INSERT INTO course (event_id, name, color, geojson, distance_m)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            event_id, name, color,
            json.dumps(geo.to_geojson_linestring(coords)), distance_m,
        ),
    )
    course_id = int(cur.lastrowid)

    conn.executemany(
        "UPDATE import_feature SET status = 'assigned', course_id = ? WHERE id = ?",
        [(course_id, fid) for fid in feature_ids],
    )
    return course_id, distance_m, warnings


def assign_poi(
    conn: sqlite3.Connection,
    event_id: int,
    feature_id: int,
    poi_type: str = "aid_station",
    name: str | None = None,
    what3words: str | None = None,
) -> int:
    """Turn one staged point feature into a POI."""
    row = get_feature(conn, feature_id)
    if row is None:
        raise ValueError(f"No staged feature with id {feature_id}")

    coords = _coords_of(row)
    if not coords:
        raise ValueError(f"Feature {feature_id} has no coordinates.")
    if row["geom_type"] != "point":
        # Lines do occasionally mark an aid station's footprint; take the center
        # rather than refusing, but say so.
        center = geo.centroid(coords)
        if center is None:
            raise ValueError(f"Feature {feature_id} has no usable position.")
        coords = [center]

    lon, lat = coords[0]
    cur = conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon, what3words, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, name or row["name"], poi_type, lat, lon,
            what3words, row["description"],
        ),
    )
    poi_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE import_feature SET status = 'assigned', poi_id = ? WHERE id = ?",
        (poi_id, feature_id),
    )
    return poi_id


def discard(conn: sqlite3.Connection, feature_ids: list[int]) -> int:
    cur = conn.executemany(
        "UPDATE import_feature SET status = 'discarded' WHERE id = ?",
        [(fid,) for fid in feature_ids],
    )
    return cur.rowcount


def suggest_event_center(
    conn: sqlite3.Connection, event_id: int
) -> LonLat | None:
    """Center of everything imported so far, for seeding the event's map view."""
    rows = conn.execute(
        "SELECT geojson FROM import_feature WHERE event_id = ? AND status != 'discarded'",
        (event_id,),
    ).fetchall()
    points: list[LonLat] = []
    for row in rows:
        geometry = json.loads(row["geojson"])
        if geometry["type"] == "Point":
            lon, lat, *_ = geometry["coordinates"]
            points.append((float(lon), float(lat)))
        else:
            points.extend(geo.from_geojson_linestring(geometry))
    return geo.centroid(points)
