"""Lead runner tracking from aid station reports."""

from __future__ import annotations

from pathlib import Path

import pytest

from courseops import db, importer, leaders, progress

COURSE = Path(__file__).parent / "fixtures" / "consumer_export_course.kml"
MILE = 1609.344


@pytest.fixture
def race(tmp_path):
    """Real course with five aid stations placed along it."""
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    importer.stage_file(conn, event_id, COURSE)
    line = next(r["id"] for r in importer.pending_features(conn, event_id)
                if r["geom_type"] == "linestring")
    course_id, _, _ = importer.assign_course(conn, event_id, [line], name="Full")

    index = progress.CourseIndex.for_event(conn, event_id)
    course = index._courses[0]

    def at_mile(mile):
        target = mile * MILE
        for i, total in enumerate(course.totals):
            if total >= target:
                return course.coords[i]
        return course.coords[-1]

    for name, mile in [("Alpha", 3.0), ("Bravo", 7.0), ("Charlie", 12.0),
                       ("Delta", 18.0), ("Echo", 23.0)]:
        lon, lat = at_mile(mile)
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', ?, ?)",
            (event_id, name, lat, lon),
        )
    return conn, event_id, course_id, index


def poi_id(conn, name):
    return conn.execute("SELECT id FROM poi WHERE name = ?", (name,)).fetchone()["id"]


def leader(conn, event_id, index, division="male"):
    return next(l for l in leaders.for_event(conn, event_id, index)
                if l.division == division)


# --- bib colours ------------------------------------------------------------

def test_bib_colour_defaults_to_the_course_line_colour(race):
    """"First yellow male" is how it gets called in, and the bibs usually match
    the route colour."""
    conn, event_id, course_id, index = race
    row = leaders.set_bib_color(conn, event_id, course_id, None, name="Orange")
    assert row["bib_color"] is not None
    assert row["bib_color"] == row["color"]
    assert row["bib_color_name"] == "Orange"


def test_bib_colour_can_differ_from_the_line_colour(race):
    conn, event_id, course_id, index = race
    row = leaders.set_bib_color(conn, event_id, course_id, "#ffcc00", "Yellow")
    assert row["bib_color"] == "#ffcc00"
    assert row["color"] != "#ffcc00"


def test_an_invalid_bib_colour_is_rejected(race):
    conn, event_id, course_id, index = race
    with pytest.raises(ValueError, match="hex colour"):
        leaders.set_bib_color(conn, event_id, course_id, "yellow")


def test_the_leader_carries_the_bib_colour(race):
    conn, event_id, course_id, index = race
    leaders.set_bib_color(conn, event_id, course_id, "#ffcc00", "Yellow")
    entry = leader(conn, event_id, index)
    assert entry.bib_color == "#ffcc00"
    assert entry.bib_color_name == "Yellow"


# --- before anything is reported --------------------------------------------

def test_with_no_sightings_the_first_station_is_what_to_expect(race):
    """An operator waiting needs to know who they are waiting for."""
    conn, event_id, course_id, index = race
    entry = leader(conn, event_id, index)

    assert entry.last_poi_id is None
    assert entry.next_poi_name == "Alpha"
    assert entry.next_distance_m / MILE == pytest.approx(3.0, abs=0.2)


def test_both_divisions_are_tracked_per_course(race):
    conn, event_id, course_id, index = race
    entries = leaders.for_event(conn, event_id, index)
    assert {e.division for e in entries} == {"male", "female"}
    assert [e.division_label if hasattr(e, "division_label") else
            leaders.division_label(e.division) for e in entries] == \
        ["First male", "First female"]


# --- recording sightings ----------------------------------------------------

def test_recording_a_sighting_moves_the_leader_forward(race):
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male",
                            poi_id(conn, "Bravo"), bib="101", by="MW")

    entry = leader(conn, event_id, index)
    assert entry.last_poi_name == "Bravo"
    assert entry.last_distance_m / MILE == pytest.approx(7.0, abs=0.2)
    assert entry.bib == "101"
    assert entry.last_by == "MW"
    # And the next station to expect them at.
    assert entry.next_poi_name == "Charlie"


def test_divisions_are_independent(race):
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Delta"))
    leaders.record_sighting(conn, event_id, course_id, "female", poi_id(conn, "Alpha"))

    assert leader(conn, event_id, index, "male").last_poi_name == "Delta"
    assert leader(conn, event_id, index, "female").last_poi_name == "Alpha"


def test_an_unknown_aid_station_is_rejected(race):
    conn, event_id, course_id, index = race
    with pytest.raises(ValueError, match="No aid station"):
        leaders.record_sighting(conn, event_id, course_id, "male", 9999)


def test_an_unknown_course_is_rejected(race):
    conn, event_id, course_id, index = race
    with pytest.raises(ValueError, match="No course"):
        leaders.record_sighting(conn, event_id, 9999, "male", poi_id(conn, "Alpha"))


def test_a_mis_tap_can_be_undone(race):
    """Race day: someone taps the wrong station."""
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Echo"))

    assert leaders.undo_last_sighting(conn, event_id, course_id, "male") is True
    assert leader(conn, event_id, index).last_poi_name == "Alpha"


def test_undo_with_nothing_recorded_is_harmless(race):
    conn, event_id, course_id, index = race
    assert leaders.undo_last_sighting(conn, event_id, course_id, "male") is False


# --- pace and estimate ------------------------------------------------------

def test_pace_needs_two_sightings(race):
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    assert leader(conn, event_id, index).pace_mps is None


def test_pace_and_eta_come_from_the_last_leg(race):
    """Alpha to Bravo is 4 miles; at 30 minutes that is an 7.5 min/mile pace,
    so Charlie (5 miles further) is about 37 minutes out."""
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Bravo"))
    ids = [r["id"] for r in leaders.sightings(conn, event_id, course_id, "male")]
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:00:00Z' WHERE id = ?",
                 (ids[0],))
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:30:00Z' WHERE id = ?",
                 (ids[1],))

    entry = leader(conn, event_id, index)

    assert entry.pace_mps == pytest.approx(4 * MILE / 1800, rel=0.1)
    assert entry.next_poi_name == "Charlie"
    assert entry.eta_seconds / 60 == pytest.approx(37, abs=6)


def test_no_eta_without_a_pace(race):
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    assert leader(conn, event_id, index).eta_seconds is None


def test_at_the_last_station_there_is_no_next(race):
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Echo"))
    entry = leader(conn, event_id, index)
    assert entry.next_poi_id is None
    assert entry.eta_seconds is None


# --- pace plausibility ------------------------------------------------------
#
# NCS enters these as they come over the net, and reports arrive in bursts when
# the net has been busy. A pace derived from two reports logged seconds apart is
# an artifact of the clock, not the runner.

def test_an_impossibly_fast_pace_is_discarded(race):
    """Two stations logged 30 seconds apart is catch-up entry, not a 120mph
    runner. No pace and no ETA beats an estimate an aid station would plan on."""
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Bravo"))
    ids = [r["id"] for r in leaders.sightings(conn, event_id, course_id, "male")]
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:00:00Z' WHERE id = ?",
                 (ids[0],))
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:00:30Z' WHERE id = ?",
                 (ids[1],))

    entry = leader(conn, event_id, index)

    assert entry.pace_mps is None
    assert entry.eta_seconds is None
    # The sighting itself is still recorded - only the derived pace is withheld.
    assert entry.last_poi_name == "Bravo"


def test_an_impossibly_slow_pace_is_discarded(race):
    """A report entered hours late would imply a walking-backwards pace."""
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Bravo"))
    ids = [r["id"] for r in leaders.sightings(conn, event_id, course_id, "male")]
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:00:00Z' WHERE id = ?",
                 (ids[0],))
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T14:00:00Z' WHERE id = ?",
                 (ids[1],))

    assert leader(conn, event_id, index).pace_mps is None


def test_a_realistic_pace_is_kept(race):
    """7:30/mile sits well inside the band."""
    conn, event_id, course_id, index = race
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Alpha"))
    leaders.record_sighting(conn, event_id, course_id, "male", poi_id(conn, "Bravo"))
    ids = [r["id"] for r in leaders.sightings(conn, event_id, course_id, "male")]
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:00:00Z' WHERE id = ?",
                 (ids[0],))
    conn.execute("UPDATE lead_sighting SET at = '2026-04-11T09:30:00Z' WHERE id = ?",
                 (ids[1],))

    entry = leader(conn, event_id, index)
    minutes_per_mile = MILE / entry.pace_mps / 60
    assert 7.0 < minutes_per_mile < 8.0
