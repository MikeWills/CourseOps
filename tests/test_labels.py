"""Pin labels.

The failure these guard against is silent and specific: every aid station on
the course labelled "A", which looks like a working feature and tells NCS
nothing. That happens the moment derivation takes the first letter blindly,
because clubs name stations "Aid 1", "Aid 2", "Aid 3".
"""

from __future__ import annotations

import pytest

from courseops import categories, db, labels


@pytest.mark.parametrize("name,expected", [
    # A number in the name is the identity, wherever it sits.
    ("Aid 3", "3"),
    ("Aid Station 12", "12"),
    ("MM 15 (FULL)", "15"),
    ("Water Stop 4", "4"),
    ("Mile 7", "7"),
    # The whole point: "Aid" describes the kind, not which one.
    ("Aid Charlie", "C"),
    ("Water Alpha", "A"),
    ("Aid Station Bravo", "B"),
    # A bare name is its own first letter.
    ("Charlie", "C"),
    ("Foxtrot", "F"),
    ("Hotel", "H"),
    ("Start", "S"),
    ("Finish", "F"),
    # Every word generic: still better than nothing, because the club asked
    # for this layer to be labelled.
    ("Water Stop", "W"),
    ("Mile marker", "M"),
    # From the real Mankato export. "(ALL)" and "(FULL)" say which race, not
    # which station - labelling a water stop "A" for ALL is confident nonsense.
    ("WATER (ALL)", "W"),
    ("Water (FULL)", "W"),
    ("Start (ALL)", "S"),
    ("END (ALL)", "E"),
    ("Exchange Zone 3 (FULL)", "3"),
    ("Water A", "A"),
    ("Water E", "E"),
    # Nothing to work with.
    ("", ""),
    ("   ", ""),
    ("!!!", ""),
])
def test_derive(name, expected):
    assert labels.derive(name) == expected


def test_derive_never_exceeds_two_characters():
    # A pin is 24px. Three characters do not fit, and text that does not fit
    # is worse than no text.
    for name in ["Aid 1234", "Checkpoint 100", "Alpha Bravo Charlie", "12345"]:
        assert len(labels.derive(name)) <= labels.MAX_LEN


def test_derive_strips_leading_zeros():
    # "Aid 03" and "Aid 3" are the same station to everyone reading the map.
    assert labels.derive("Aid 03") == "3"


def test_numbered_stations_do_not_all_collapse_to_one_letter():
    """The regression this whole module exists for."""
    got = [labels.derive(n) for n in ["Aid 1", "Aid 2", "Aid 3", "Aid 4"]]
    assert got == ["1", "2", "3", "4"]
    assert len(set(got)) == 4


def test_phonetic_stations_keep_their_letters():
    got = [labels.derive(n) for n in ["Alpha", "Bravo", "Charlie", "Delta"]]
    assert got == ["A", "B", "C", "D"]


def test_override_wins_and_is_trimmed():
    assert labels.for_poi("Aid 3", "XY") == "XY"
    assert labels.for_poi("Aid 3", "  Q ") == "Q"
    # Too long is truncated rather than rejected: the map cannot show more.
    assert labels.for_poi("Aid 3", "LONG") == "LO"
    # Empty override falls back to the guess.
    assert labels.for_poi("Aid 3", "") == "3"
    assert labels.for_poi("Aid 3", None) == "3"


def test_override_case_is_kept_as_typed():
    assert labels.for_poi("Aid 3", "c") == "c"


# --- the per-layer flag ----------------------------------------------------

def _event(tmp_path):
    conn = db.connect(tmp_path / "labels.sqlite3")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO event (slug, name, timezone) VALUES ('e', 'E', 'UTC')")
    return conn, conn.execute("SELECT id FROM event").fetchone()["id"]


def test_labels_default_on_for_staffed_layers_only(tmp_path):
    """A club should not have to switch this on to get the obvious behaviour.

    Aid stations are the pins someone needs to tell apart; mile markers are
    the ones that would bury the map.
    """
    conn, event_id = _event(tmp_path)
    rows = {r["key"]: r for r in categories.poi_categories(conn, event_id)}
    assert rows["aid_station"]["show_labels"] == 1
    assert rows["start"]["show_labels"] == 1
    assert rows["parking"]["show_labels"] == 0
    assert rows["medical"]["show_labels"] == 0


def test_layer_created_by_import_starts_unlabelled(tmp_path):
    """48 mile markers arriving from a file must not switch labels on."""
    conn, event_id = _event(tmp_path)
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'MM 1', 'mile_markers', 1.0, 2.0)", (event_id,))
    rows = {r["key"]: r for r in categories.poi_categories(conn, event_id)}
    assert rows["mile_markers"]["show_labels"] == 0


def test_show_labels_is_editable(tmp_path):
    conn, event_id = _event(tmp_path)
    categories.poi_categories(conn, event_id)          # seed
    row = categories.update_poi_category(
        conn, event_id, "parking", {"show_labels": True})
    assert row["show_labels"] == 1
    row = categories.update_poi_category(
        conn, event_id, "parking", {"show_labels": False})
    assert row["show_labels"] == 0


def test_existing_database_gets_labels_on_for_staffed_layers(tmp_path):
    """The migration backfill.

    ALTER TABLE can only take a constant default, so without a backfill this
    feature would arrive switched off for every event that already exists -
    which is everyone who has already set up a race.
    """
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO event (slug, name, timezone) VALUES ('e', 'E', 'UTC')")
    event_id = conn.execute("SELECT id FROM event").fetchone()["id"]
    categories.poi_categories(conn, event_id)          # seed
    # Simulate the older database that predates the column.
    conn.execute("ALTER TABLE poi_category DROP COLUMN show_labels")
    assert "show_labels" not in {
        r["name"] for r in conn.execute("PRAGMA table_info(poi_category)")
    }
    conn.close()

    conn = db.connect(path)
    applied = db.init_schema(conn)
    assert "poi_category.show_labels" in applied
    rows = {r["key"]: r for r in categories.poi_categories(conn, event_id)}
    assert rows["aid_station"]["show_labels"] == 1
    assert rows["parking"]["show_labels"] == 0
