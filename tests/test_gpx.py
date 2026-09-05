"""GPX import: a second way in to the same staging (GitHub issue #1).

Consumer route tools often export GPX and nothing else. The parser feeds the
existing two-phase import, so these tests check three things: the file is read
into the same features KML produces, coordinates come out (lon, lat) despite
GPX writing them lat-first, and nothing downstream changed - staging, review
and stitching behave exactly as they do for KML.
"""

from pathlib import Path

import pytest

from courseops import db, gpx, importer, kml

FIXTURE = Path(__file__).parent / "fixtures" / "route.gpx"


@pytest.fixture()
def features():
    return kml.load(FIXTURE)


def by_name(features, name):
    return next(f for f in features if f.name == name)


# --- reading ----------------------------------------------------------------

def test_every_element_kind_is_found(features):
    """4 waypoints, 1 route, 3 track segments, 1 nameless track."""
    assert len(features) == 9
    assert sum(f.geom_type == "point" for f in features) == 4
    assert sum(f.geom_type == "linestring" for f in features) == 5


def test_coordinates_are_lon_lat_despite_gpx_writing_lat_first(features):
    """The one place a silent transposition is easiest to introduce."""
    water = by_name(features, "Water Stop 1")
    assert water.point == (-86.575, 34.732)
    route = by_name(features, "10K")
    assert route.coords[0] == (-86.580, 34.730)


def test_desc_is_the_description_and_cmt_is_the_fallback(features):
    assert by_name(features, "Water Stop 1").description == "Corner of Oak and 3rd"
    assert by_name(features, "Parking").description == "Overflow lot, opens 5am"
    assert by_name(features, "Start").description is None


def test_a_track_stages_one_feature_per_segment_numbered(features):
    """Numbered like a KML MultiGeometry, so the review list can tell them
    apart and the existing stitching joins them."""
    names = sorted(f.name for f in features if f.name.startswith("Half"))
    assert names == ["Half Marathon [1]", "Half Marathon [2]", "Half Marathon [3]"]


def test_elements_without_a_name_still_stage(features):
    """A nameless waypoint and a nameless track: staged, not dropped. The
    label is what the review screen shows, and it copes with an empty name."""
    nameless = [f for f in features if f.name == ""]
    assert {f.geom_type for f in nameless} == {"point", "linestring"}
    assert all(f.label for f in nameless)


def test_elevation_and_time_are_ignored(features):
    seg = by_name(features, "Half Marathon [1]")
    assert all(len(c) == 2 for c in seg.coords)


def test_there_is_no_folder_so_suggestions_stay_conservative(features):
    assert all(f.folder == "" for f in features)
    # A bare "Half Marathon [2]" has a course hint in its name; the nameless
    # track has nothing and must not be guessed.
    assert by_name(features, "Half Marathon [2]").suggest() == "course"
    nameless_line = next(f for f in features
                         if f.name == "" and f.geom_type == "linestring")
    assert nameless_line.suggest() == "unassigned"


def test_a_dense_recording_is_flagged_not_thinned(tmp_path):
    """Import never discards fidelity. It says the file looks like a recording
    and leaves simplification as a separate decision."""
    pts = "".join(
        f'<trkpt lat="{34.7 + i * 1e-5:.5f}" lon="-86.6"></trkpt>'
        for i in range(gpx.DENSE_TRACK_POINTS + 1)
    )
    path = tmp_path / "run.gpx"
    path.write_text(f'<gpx><trk><name>Run</name><trkseg>{pts}</trkseg></trk></gpx>')
    [track] = kml.load(path)
    assert len(track.coords) == gpx.DENSE_TRACK_POINTS + 1
    assert any("recording" in w for w in track.warnings)


def test_reversed_lat_lon_is_flagged_not_accepted(tmp_path):
    path = tmp_path / "swapped.gpx"
    # Mankato with lat and lon swapped: a "latitude" of -93.99 is impossible.
    path.write_text('<gpx><wpt lat="-93.99" lon="44.13"><name>X</name></wpt>'
                    '<wpt lat="44.13" lon="-93.99"><name>Y</name></wpt></gpx>')
    features = kml.load(path)
    assert [f.name for f in features] == ["Y"]


def test_a_gpx_with_the_wrong_extension_is_still_read(tmp_path):
    """Dispatch is on the root element, not the filename. A phone that saved
    route.gpx as route.xml, or a KML renamed .gpx, both still work."""
    path = tmp_path / "route.xml"
    path.write_bytes(FIXTURE.read_bytes())
    assert len(kml.load(path)) == 9


def test_a_gpx_with_nothing_in_it_raises(tmp_path):
    empty = tmp_path / "empty.gpx"
    empty.write_text("<gpx><metadata><name>Empty</name></metadata></gpx>")
    with pytest.raises(kml.KmlError, match="GPX with no tracks"):
        kml.load(empty)


# --- hardening: same posture as KML - third-party files, uploaded -----------

def test_entity_expansion_is_rejected(tmp_path):
    bomb = tmp_path / "bomb.gpx"
    bomb.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE gpx ['
        '<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        ']><gpx><wpt lat="1" lon="1"><name>&c;</name></wpt></gpx>'
    )
    with pytest.raises(kml.KmlError):
        kml.load(bomb)


def test_external_entity_is_rejected(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive")
    xxe = tmp_path / "xxe.gpx"
    xxe.write_text(
        '<?xml version="1.0"?>'
        f'<!DOCTYPE gpx [<!ENTITY xxe SYSTEM "file://{secret.as_posix()}">]>'
        '<gpx><wpt lat="1" lon="1"><name>&xxe;</name></wpt></gpx>'
    )
    with pytest.raises(kml.KmlError):
        kml.load(xxe)


def test_oversized_gpx_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(kml, "MAX_KML_BYTES", 1024)
    big = tmp_path / "big.gpx"
    big.write_bytes(b"<gpx>" + b" " * 2048 + b"</gpx>")
    with pytest.raises(kml.KmlError, match="limit"):
        kml.load(big)


# --- staging: identical to KML ---------------------------------------------

@pytest.fixture()
def event_db(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    return conn, db.create_event(conn, "m2026", "Spring Marathon")


def test_stage_file_records_gpx_as_the_source_kind(event_db):
    conn, event_id = event_db
    summary = importer.stage_file(conn, event_id, FIXTURE)
    assert summary.total == 9
    kind = conn.execute("SELECT source_kind FROM import_batch").fetchone()[0]
    assert kind == "gpx"


def test_nothing_reaches_course_or_poi_without_assignment(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    assert conn.execute("SELECT COUNT(*) FROM course").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 0
    pending = importer.pending_features(conn, event_id)
    assert len(pending) == 9


def test_track_segments_stitch_including_the_reversed_one(event_db):
    """Middle segment listed first, last one drawn backwards - the exact
    shape that once folded a KML course back on itself."""
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    ids = [row["id"] for row in importer.pending_features(conn, event_id)
           if row["name"].startswith("Half Marathon")]

    course_id, distance_m, warnings = importer.assign_course(
        conn, event_id, ids, name="Half")

    assert not warnings
    row = conn.execute("SELECT geojson FROM course WHERE id = ?", (course_id,)).fetchone()
    import json
    coords = json.loads(row["geojson"])["coordinates"]
    # One continuous line from the true start to the true end, no doubling.
    assert coords[0] == [-86.58, 34.73]
    assert coords[-1] == [-86.55, 34.76]
    assert len(coords) == 7


def test_a_waypoint_becomes_a_place(event_db):
    conn, event_id = event_db
    importer.stage_file(conn, event_id, FIXTURE)
    water = next(row for row in importer.pending_features(conn, event_id)
                 if row["name"] == "Water Stop 1")
    poi_id = importer.assign_poi(conn, event_id, water["id"], "aid_station")
    row = conn.execute("SELECT * FROM poi WHERE id = ?", (poi_id,)).fetchone()
    assert (row["lon"], row["lat"]) == (-86.575, 34.732)
    assert row["notes"] == "Corner of Oak and 3rd"
