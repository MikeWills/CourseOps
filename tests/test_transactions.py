"""A refused change writes nothing.

The connection is autocommit, so each statement used to be its own
transaction and `conn.commit()` was a no-op that read as if the statements
before it were one unit. They were not: `create_poi` inserted the place and
THEN validated the What3Words address, so a 400 left a place behind it, and
the officer who corrected the address and submitted again had two "Water
Stop C" pins. `db.transaction` is the fix; these tests pin the shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from courseops import admin, categories, db, importer

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    return conn, db.create_event(conn, "e", "Event")


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_transaction_rolls_back_on_an_exception(event):
    conn, event_id = event
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
                " VALUES (?, 'A', 'aid_station', 44.1, -93.9)", (event_id,))
            assert count(conn, "poi") == 1
            raise RuntimeError("half way")
    assert count(conn, "poi") == 0
    assert not conn.in_transaction


def test_a_transaction_commits_and_nests(event):
    """An inner `transaction` inside an outer one joins it rather than
    committing early: `assign_features` wraps several `assign_poi` calls,
    each of which wraps itself for the CLI's sake."""
    conn, event_id = event
    with db.transaction(conn):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
                " VALUES (?, 'A', 'aid_station', 44.1, -93.9)", (event_id,))
        assert conn.in_transaction   # the inner block did not commit
    assert not conn.in_transaction
    assert count(conn, "poi") == 1


def test_a_second_connection_sees_nothing_until_commit(tmp_path):
    """The point of the write lock: the ingest task and every request write
    on their own connections during an event."""
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    other = db.connect(path)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, 'A', 'aid_station', 44.1, -93.9)", (event_id,))
        assert count(other, "poi") == 0
    assert count(other, "poi") == 1


def test_a_refused_place_is_not_created(event):
    """The reproduction: a bad What3Words address after a valid INSERT."""
    conn, event_id = event
    with pytest.raises(ValueError, match="What3Words"):
        admin.create_poi(conn, event_id, {
            "name": "Water Stop C", "poi_type": "aid_station",
            "lat": 44.1, "lon": -93.9, "what3words": "not-an-address"})
    assert count(conn, "poi") == 0

    with pytest.raises(ValueError, match="course"):
        admin.create_poi(conn, event_id, {
            "name": "Water Stop C", "poi_type": "aid_station",
            "lat": 44.1, "lon": -93.9, "course_ids": [999]})
    assert count(conn, "poi") == 0


def test_a_refused_roster_save_changes_nothing(event):
    """A rename followed by a bad posting used to leave the rename behind
    the 400."""
    conn, event_id = event
    db.upsert_roster_entry(conn, event_id, "N0CALL-1", "Aid 3", "aid_station")
    with pytest.raises(ValueError, match="POI"):
        admin.save_roster_entry(conn, event_id, {
            "station_key": "N0CALL-7", "original_station_key": "N0CALL-1",
            "display_label": "Aid 3", "category": "aid_station",
            "poi_id": 999})
    assert [r["station_key"] for r in db.roster_for_event(conn, event_id)] \
        == ["N0CALL-1"]


def test_a_refused_course_assignment_stages_nothing(event):
    """Two features, the second a point: the course row must not exist and
    the first feature must still be pending."""
    conn, event_id = event
    importer.stage_file(conn, event_id, FIXTURE)
    features = importer.pending_features(conn, event_id)
    line = next(r["id"] for r in features if r["geom_type"] == "linestring")
    point = next(r["id"] for r in features if r["geom_type"] == "point")

    with pytest.raises(ValueError):
        admin.assign_features(conn, event_id, {
            "kind": "course", "ids": [line, point], "name": "Half"})
    assert count(conn, "course") == 0

    # Several points at once, one into a layer that does not exist: none of
    # the others land either, or the review screen shows a partial result
    # that looks like it worked.
    points = [r["id"] for r in features if r["geom_type"] == "point"]
    categories.add_poi_category(conn, event_id, "Water")
    with pytest.raises(ValueError, match="layer"):
        admin.assign_features(conn, event_id, {
            "kind": "poi", "ids": points, "poi_type": "no_such_layer"})
    assert count(conn, "poi") == 0
    assert all(r["status"] == "pending"
               for r in importer.pending_features(conn, event_id, True))


def test_a_failed_reorder_leaves_the_order_alone(event):
    conn, event_id = event
    for name in ("A", "B", "C"):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', 44.1, -93.9)", (event_id, name))
    ids = [r["id"] for r in conn.execute("SELECT id FROM poi").fetchall()]
    admin.reorder_pois(conn, event_id, ids)
    before = conn.execute("SELECT id, sort_order FROM poi").fetchall()

    with pytest.raises(ValueError):
        admin.reorder_pois(conn, event_id, [ids[2], ids[0], 999])
    assert conn.execute("SELECT id, sort_order FROM poi").fetchall() == before


def test_the_decorative_commits_are_gone():
    """`conn.commit()` on an autocommit connection did nothing, and read as
    if the statements before it were one unit. Worse, inside an explicit
    transaction it WOULD commit - early. There must be none."""
    src = Path(admin.__file__).parent
    offenders = [
        p.name for p in src.glob("*.py")
        if "conn.commit()" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
