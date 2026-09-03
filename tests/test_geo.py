"""Geometry tests. Coordinates are (lon, lat) throughout, per GeoJSON and KML."""

from __future__ import annotations

import pytest

from courseops import geo


def test_haversine_against_a_known_distance():
    # One degree of latitude is ~111.2 km anywhere on the globe.
    d = geo.haversine_m((-86.58, 34.0), (-86.58, 35.0))
    assert 111_000 < d < 111_400


def test_line_length_sums_segments():
    coords = [(-86.58, 34.73), (-86.57, 34.73), (-86.56, 34.73)]
    total = geo.line_length_m(coords)
    leg = geo.haversine_m(coords[0], coords[1])
    assert total == pytest.approx(leg * 2, rel=1e-6)


def test_dedupe_removes_segment_join_duplicates():
    coords = [(-86.58, 34.73), (-86.58, 34.73), (-86.57, 34.73)]
    assert len(geo.dedupe_consecutive(coords)) == 2


def test_stitch_reverses_a_backwards_segment():
    """Organizer KML routinely has one segment drawn finish-to-start."""
    first = [(-86.58, 34.730), (-86.57, 34.735)]
    second_backwards = [(-86.56, 34.741), (-86.57, 34.735)]

    joined, warnings = geo.stitch([first, second_backwards])

    assert joined[0] == (-86.58, 34.730)
    assert joined[-1] == (-86.56, 34.741)
    assert warnings == []


def test_stitch_reorders_segments():
    """Segments arrive in arbitrary order in the file."""
    a = [(-86.58, 34.730), (-86.57, 34.735)]
    b = [(-86.57, 34.735), (-86.56, 34.741)]

    joined, warnings = geo.stitch([b, a])  # deliberately out of order

    assert len(joined) == 3
    assert warnings == []


def test_stitch_bridges_a_modest_gap_and_says_so():
    """A short gap is a digitising artefact, worth importing and eyeballing."""
    a = [(-86.58, 34.73), (-86.57, 34.73)]
    slightly_apart = [(-86.565, 34.73), (-86.560, 34.73)]

    joined, warnings = geo.stitch([a, slightly_apart])

    assert len(joined) == 4
    assert any("Gap of" in w for w in warnings)


def test_stitch_refuses_to_invent_a_kilometres_long_leg():
    """The real case this exists for: an organizer's marathon arrived as eight
    LineStrings, five chaining cleanly into 26.12 miles and three being chutes
    of 0.14, 0.01 and 0.09 miles near the start. Dragging those in cost a 5.4 km
    fabricated leg and reported a 29.76 mile marathon - confident, plausible and
    wrong, which is the one thing this must never print."""
    a = [(-86.58, 34.73), (-86.57, 34.73)]
    far_away = [(-86.20, 34.73), (-86.19, 34.73)]

    joined, warnings = geo.stitch([a, far_away])

    assert joined == a
    assert any("could not be joined" in w for w in warnings)
    # The warning has to name what was dropped, or it is just a shrug.
    assert any("mi" in w for w in warnings)


def test_stitch_handles_a_single_segment():
    coords = [(-86.58, 34.73), (-86.57, 34.73)]
    joined, warnings = geo.stitch([coords])
    assert joined == coords
    assert warnings == []


def test_stitch_ignores_degenerate_segments():
    joined, warnings = geo.stitch([[(-86.58, 34.73)]])
    assert joined == []
    assert warnings


def test_geojson_roundtrip():
    coords = [(-86.58, 34.73), (-86.57, 34.74)]
    assert geo.from_geojson_linestring(geo.to_geojson_linestring(coords)) == coords


def test_centroid_of_a_course():
    coords = [(-86.60, 34.70), (-86.50, 34.80)]
    assert geo.centroid(coords) == pytest.approx((-86.55, 34.75))


def test_centroid_of_nothing_is_none():
    assert geo.centroid([]) is None


def test_stitch_grows_from_the_front_when_the_middle_segment_is_first():
    """Regression: growing only from the tail folded the course back on itself.

    Given a middle segment first, the piece belonging at the front must be
    prepended, not reversed onto the back.
    """
    middle = [(-86.57, 34.735), (-86.56, 34.741)]
    front = [(-86.58, 34.730), (-86.57, 34.735)]
    back = [(-86.56, 34.741), (-86.55, 34.748)]

    joined, warnings = geo.stitch([middle, front, back])

    assert warnings == []
    assert joined[0] == (-86.58, 34.730)
    assert joined[-1] == (-86.55, 34.748)
    assert len(joined) == 4
    # Strictly eastward: a folded line would go back west somewhere.
    lons = [lon for lon, _ in joined]
    assert lons == sorted(lons)
