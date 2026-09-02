"""Storage is metric; presentation is US customary. Verify the seam."""

from aprswebtracker import units


def test_speed_conversion():
    assert units.kmh_to_mph(160.9344) == 100.0
    assert units.format_speed(16.09344) == "10 mph"
    assert units.format_speed(None) == "--"


def test_altitude_conversion():
    assert round(units.meters_to_feet(304.8)) == 1000
    assert units.format_altitude(304.8) == "1,000 ft"


def test_course_distance_reads_in_miles():
    """'mile 14.2' is the unit the net speaks."""
    assert units.format_distance(22852.0) == "14.2 mi"
    assert units.format_distance(22852.0, imperial=False) == "22.9 km"
