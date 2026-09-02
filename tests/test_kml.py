"""KML/KMZ parsing tests, driven by a fixture reproducing real organizer defects."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from aprswebtracker import kml

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def features():
    return kml.load(FIXTURE)


def by_name(features, fragment):
    return next(f for f in features if fragment.lower() in f.name.lower())


def test_finds_every_placemark(features):
    # 4 course placemarks (one a MultiGeometry of 2) + 6 support points
    assert len(features) == 11
    assert sum(1 for f in features if f.geom_type == "point") == 6
    assert sum(1 for f in features if f.geom_type == "linestring") == 5


def test_folder_path_is_kept(features):
    """When a placemark is named 'Untitled Path', the folder is the only clue."""
    untitled = by_name(features, "Untitled Path")
    assert untitled.folder == "Spring Marathon 2026 / Courses"
    assert not untitled.has_useful_name
    assert "(unnamed)" in untitled.label


def test_multigeometry_splits_into_numbered_features(features):
    parts = [f for f in features if f.name.startswith("10K Route")]
    assert len(parts) == 2
    assert {f.name for f in parts} == {"10K Route [1]", "10K Route [2]"}


def test_coordinates_tolerate_whitespace_and_altitude(features):
    part1 = by_name(features, "Half Marathon - Part 1")
    assert part1.coords[0] == (-86.58, 34.73)
    assert len(part1.coords) == 3


def test_point_without_altitude_parses(features):
    aid2 = by_name(features, "Aid Station 2")
    assert aid2.point == (-86.57, 34.735)


def test_extended_data_does_not_break_parsing(features):
    start = by_name(features, "Start / Finish")
    assert start.geom_type == "point"


def test_suggestions_are_conservative(features):
    assert by_name(features, "Water Stop 1").suggest() == "poi:aid_station"
    assert by_name(features, "Aid Station 2").suggest() == "poi:aid_station"
    assert by_name(features, "Medical Tent").suggest() == "poi:medical"
    assert by_name(features, "Volunteer Parking").suggest() == "poi:parking"
    assert by_name(features, "Start / Finish").suggest() == "poi:start_finish"
    assert by_name(features, "Half Marathon - Part 1").suggest() == "course"
    # A porta-john matches no hint and must not be guessed into something.
    assert by_name(features, "Porta-John").suggest() == "unassigned"


def test_folder_supplies_the_hint_when_the_name_does_not(features):
    """An unnamed line inside a 'Courses' folder is suggested as a course.

    This is the folder path earning its keep: the placemark itself says nothing.
    The suggestion is still advisory — this particular line is nowhere near the
    other routes, which is exactly the sort of thing the human review step is
    there to catch.
    """
    assert by_name(features, "Untitled Path").suggest() == "course"


def test_unhinted_line_outside_a_course_folder_is_not_guessed():
    raw = b"""<kml><Document><Folder><name>Misc</name><Placemark>
        <name>Untitled Path</name><LineString>
        <coordinates>-86.5,34.7 -86.4,34.6</coordinates>
        </LineString></Placemark></Folder></Document></kml>"""
    assert kml.parse_kml_bytes(raw)[0].suggest() == "unassigned"


def test_kmz_is_read(tmp_path):
    """KMZ is a zip; the payload is usually but not always named doc.kml."""
    kmz = tmp_path / "course.kmz"
    with zipfile.ZipFile(kmz, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(FIXTURE, "doc.kml")
    assert len(kml.load(kmz)) == 11


def test_kmz_with_oddly_named_payload(tmp_path):
    kmz = tmp_path / "course.kmz"
    with zipfile.ZipFile(kmz, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(FIXTURE, "files/SpringMarathon.kml")
    assert len(kml.load(kmz)) == 11


def test_namespaceless_kml_parses():
    """Some exporters omit the namespace entirely."""
    raw = b"""<kml><Document><Placemark><name>X</name>
        <Point><coordinates>-86.5,34.7</coordinates></Point>
        </Placemark></Document></kml>"""
    features = kml.parse_kml_bytes(raw)
    assert len(features) == 1
    assert features[0].point == (-86.5, 34.7)


def test_reversed_coordinates_are_flagged_not_silently_accepted():
    """lat,lon instead of lon,lat is a common export bug."""
    coords, warnings = kml.parse_coordinates("34.73,-86.58 200.0,99.0")
    assert coords == [(34.73, -86.58)]  # first pair is in range, if wrong
    assert any("out of range" in w for w in warnings)


def test_missing_file_raises_kmlerror(tmp_path):
    with pytest.raises(kml.KmlError):
        kml.load(tmp_path / "nope.kml")


def test_non_xml_raises_kmlerror(tmp_path):
    bad = tmp_path / "bad.kml"
    bad.write_text("this is not xml")
    with pytest.raises(kml.KmlError, match="Not valid XML"):
        kml.load(bad)


def test_kml_with_no_geometry_raises(tmp_path):
    empty = tmp_path / "styles.kml"
    empty.write_text('<kml><Document><Style id="a"/></Document></kml>')
    with pytest.raises(kml.KmlError, match="No placemarks"):
        kml.load(empty)


# --- hardening: these files come from third parties and are uploaded ---------

def test_entity_expansion_is_rejected(tmp_path):
    """Billion laughs. defusedxml must refuse this rather than expand it."""
    bomb = tmp_path / "bomb.kml"
    bomb.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE kml ['
        '<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        ']><kml><Document><name>&c;</name></Document></kml>'
    )
    with pytest.raises(kml.KmlError):
        kml.load(bomb)


def test_external_entity_is_rejected(tmp_path):
    """XXE: must not read a local file off disk."""
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive")
    xxe = tmp_path / "xxe.kml"
    xxe.write_text(
        '<?xml version="1.0"?>'
        f'<!DOCTYPE kml [<!ENTITY xxe SYSTEM "file://{secret.as_posix()}">]>'
        '<kml><Document><name>&xxe;</name></Document></kml>'
    )
    with pytest.raises(kml.KmlError):
        kml.load(xxe)


def test_zip_bomb_is_rejected(tmp_path):
    """defusedxml guards the XML parse but not the unzip that precedes it."""
    kmz = tmp_path / "bomb.kmz"
    with zipfile.ZipFile(kmz, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", b"\0" * (80 * 1024 * 1024))
    with pytest.raises(kml.KmlError, match="compression ratio|limit"):
        kml.load(kmz)
