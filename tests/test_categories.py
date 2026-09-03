"""Layers and role names belong to the club, not to the code.

A KML arrives with whatever the organizer drew — mile markers, medical, traffic
control, portable toilets — and the next club will have a different set.
Hardcoding the list means editing Python to accept a race.

The load-bearing property is `staffed`: we put a person here and track them.
Before it existed the code asked `poi_type == 'aid_station'`, which meant a club
renaming its layer to "Water Stops" silently lost lead runner tracking.
"""

import pytest

from courseops import categories, db, leaders


@pytest.fixture()
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    return conn, db.create_event(conn, "e", "Event")


def _poi(conn, event_id, name, kind, lat=44.1, lon=-93.9):
    return conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon) VALUES (?,?,?,?,?)",
        (event_id, name, kind, lat, lon),
    ).lastrowid


# --- the set is open --------------------------------------------------------

def test_a_new_event_starts_with_the_usual_layers(event):
    conn, event_id = event
    keys = {c["key"] for c in categories.poi_categories(conn, event_id)}
    assert {"aid_station", "medical", "parking"} <= keys


def test_a_club_can_add_layers_of_its_own(event):
    conn, event_id = event
    for name in ("Mile markers", "Traffic control", "Portable toilets",
                 "Spectator zones", "Timing mats"):
        categories.add_poi_category(conn, event_id, name)

    keys = {c["key"] for c in categories.poi_categories(conn, event_id)}
    assert {"mile_markers", "traffic_control", "portable_toilets",
            "spectator_zones", "timing_mats"} <= keys


def test_there_is_no_limit_on_how_many(event):
    """The point of the whole change: nothing caps this."""
    conn, event_id = event
    for i in range(40):
        categories.add_poi_category(conn, event_id, f"Layer {i}")

    assert len(categories.poi_categories(conn, event_id)) >= 40


def test_the_name_becomes_a_stable_key(event):
    conn, event_id = event
    row = categories.add_poi_category(conn, event_id, "Traffic Control!")
    assert row["key"] == "traffic_control"


def test_two_layers_cannot_share_a_key(event):
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Traffic control")
    with pytest.raises(categories.CategoryError):
        categories.add_poi_category(conn, event_id, "traffic  control")


def test_a_layer_needs_a_name(event):
    conn, event_id = event
    with pytest.raises(categories.CategoryError):
        categories.add_poi_category(conn, event_id, "   ")


# --- renaming ---------------------------------------------------------------

def test_renaming_a_layer_does_not_move_its_places(event):
    """The key is what poi.poi_type holds, so a rename is display only. If it
    moved, every place in the layer would belong to nothing."""
    conn, event_id = event
    _poi(conn, event_id, "Alpha", "aid_station")

    row = categories.update_poi_category(
        conn, event_id, "aid_station", {"name": "Water Stops"})

    assert row["key"] == "aid_station"
    assert row["name"] == "Water Stops"
    kept = conn.execute(
        "SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = 'aid_station'",
        (event_id,),
    ).fetchone()["c"]
    assert kept == 1


def test_a_role_can_be_renamed(event):
    conn, event_id = event
    categories.rename_roster_role(conn, event_id, "rover", "Floater")
    assert categories.role_labels(conn, event_id)["rover"] == "Floater"


def test_renaming_a_role_keeps_its_status_wording(event):
    """The keys are fixed because each carries its own vocabulary. A club
    calling sweeps something else still gets "Finished", not "Torn down"."""
    conn, event_id = event
    categories.rename_roster_role(conn, event_id, "sweep", "Tail-end Charlie")

    assert db.op_status_label("sweep", "closed") == "Finished"
    assert db.op_status_label("aid_station", "closed") == "Torn down"


def test_an_unknown_role_cannot_be_renamed(event):
    conn, event_id = event
    with pytest.raises(categories.CategoryError):
        categories.rename_roster_role(conn, event_id, "made_up", "Nope")


# --- staffed ----------------------------------------------------------------

def test_staffed_is_what_marks_a_layer_operational(event):
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Mile markers", staffed=False)
    categories.add_poi_category(conn, event_id, "Traffic control", staffed=True)

    staffed = categories.staffed_keys(conn, event_id)
    assert "traffic_control" in staffed
    assert "mile_markers" not in staffed


def test_medical_is_unstaffed_by_default(event):
    """A medic tent is run by the race's own medical team, not by an operator
    we track. A club that does staff them ticks the box."""
    conn, event_id = event
    assert "medical" not in categories.staffed_keys(conn, event_id)


def test_a_renamed_layer_keeps_lead_runner_tracking(event):
    """The regression this flag exists for. Lead runner sightings used to be
    filtered by poi_type == 'aid_station', so renaming the layer silently
    emptied the sighting list."""
    conn, event_id = event
    _poi(conn, event_id, "Alpha", "aid_station")
    categories.update_poi_category(
        conn, event_id, "aid_station", {"name": "Water Stops"})

    assert leaders.staffed_places(conn, event_id)


def test_an_unstaffed_layer_is_not_offered_for_sightings(event):
    """Nobody is standing at a portable toilet to see a runner go past."""
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Portable toilets", staffed=False)
    _poi(conn, event_id, "Toilets A", "portable_toilets")

    names = {p["name"] for p in leaders.staffed_places(conn, event_id)}
    assert "Toilets A" not in names


def test_making_a_layer_staffed_brings_it_in(event):
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Traffic control", staffed=False)
    _poi(conn, event_id, "5th & Main", "traffic_control")
    assert not leaders.staffed_places(conn, event_id)

    categories.update_poi_category(
        conn, event_id, "traffic_control", {"staffed": True})

    names = {p["name"] for p in leaders.staffed_places(conn, event_id)}
    assert "5th & Main" in names


# --- deleting ---------------------------------------------------------------

def test_a_layer_in_use_will_not_delete(event):
    """Deleting it would leave its places drawn in no layer at all: in the
    database, off the map, and no error to say so."""
    conn, event_id = event
    _poi(conn, event_id, "Alpha", "aid_station")

    assert categories.delete_poi_category(conn, event_id, "aid_station") == 1
    assert categories.get_poi_category(conn, event_id, "aid_station") is not None


def test_an_empty_layer_deletes(event):
    conn, event_id = event
    assert categories.delete_poi_category(conn, event_id, "parking") == 0
    with pytest.raises(categories.CategoryError):
        categories.get_poi_category(conn, event_id, "parking")


# --- upgrading an existing database -----------------------------------------

def test_places_imported_under_an_unknown_key_still_get_a_layer(event):
    """From an older database or a CLI import. Better an unnamed layer the club
    can rename than a place that belongs to nothing and never draws."""
    conn, event_id = event
    _poi(conn, event_id, "Somewhere", "porta_potty")

    keys = {c["key"] for c in categories.poi_categories(conn, event_id)}
    assert "porta_potty" in keys
    row = categories.get_poi_category(conn, event_id, "porta_potty")
    assert row["name"] == "Porta Potty"
    assert not row["staffed"]


def test_seeding_twice_does_not_undo_a_rename(event):
    conn, event_id = event
    categories.update_poi_category(
        conn, event_id, "aid_station", {"name": "Water Stops"})

    categories.seed_poi_categories(conn, event_id)

    assert categories.get_poi_category(conn, event_id, "aid_station")["name"] \
        == "Water Stops"


def test_a_deleted_default_layer_stays_deleted(event):
    """Re-seeding used to resurrect it: a club would remove "Parking" and find
    it back on the next page load, which teaches people not to trust the
    screen."""
    conn, event_id = event
    assert categories.delete_poi_category(conn, event_id, "parking") == 0

    categories.poi_categories(conn, event_id)   # the lazy seed runs here
    categories.seed_poi_categories(conn, event_id)

    keys = {c["key"] for c in categories.poi_categories(conn, event_id)}
    assert "parking" not in keys


# --- sorting a flat import --------------------------------------------------

def test_places_can_be_moved_between_layers(event):
    """Organizer KML is usually one flat list, so everything lands in a single
    layer and has to be sorted afterwards."""
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Medical tents", staffed=False)
    a = _poi(conn, event_id, "Medic Alpha", "aid_station")
    b = _poi(conn, event_id, "Medic Bravo", "aid_station")
    keep = _poi(conn, event_id, "Ham Alpha", "aid_station")

    from courseops import admin
    moved = admin.move_pois(conn, event_id, [a, b], "medical_tents")

    assert moved == 2
    rows = {r["name"]: r["poi_type"] for r in conn.execute(
        "SELECT name, poi_type FROM poi WHERE event_id = ?", (event_id,))}
    assert rows == {"Medic Alpha": "medical_tents",
                    "Medic Bravo": "medical_tents",
                    "Ham Alpha": "aid_station"}


def test_moving_to_a_layer_that_does_not_exist_is_refused(event):
    """It would leave the place drawn in no layer - in the database, off the
    map, with nothing to say why."""
    conn, event_id = event
    poi_id = _poi(conn, event_id, "Somewhere", "aid_station")

    from courseops import admin
    with pytest.raises(categories.CategoryError):
        admin.move_pois(conn, event_id, [poi_id], "not_a_layer")


def test_moving_nothing_is_refused(event):
    conn, event_id = event
    from courseops import admin
    with pytest.raises(ValueError):
        admin.move_pois(conn, event_id, [], "aid_station")


def test_a_single_place_can_change_layer(event):
    conn, event_id = event
    categories.add_poi_category(conn, event_id, "Traffic control", staffed=True)
    poi_id = _poi(conn, event_id, "5th & Main", "aid_station")

    from courseops import admin
    admin.update_poi(conn, event_id, poi_id, {"poi_type": "traffic_control"})

    assert conn.execute(
        "SELECT poi_type FROM poi WHERE id = ?", (poi_id,)
    ).fetchone()["poi_type"] == "traffic_control"


def test_an_unknown_layer_is_refused_on_a_single_edit(event):
    conn, event_id = event
    poi_id = _poi(conn, event_id, "Somewhere", "aid_station")
    from courseops import admin
    with pytest.raises(categories.CategoryError):
        admin.update_poi(conn, event_id, poi_id, {"poi_type": "invented"})
