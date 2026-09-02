"""Course styling: color, line style, and draw order."""

from __future__ import annotations

from pathlib import Path

import pytest

from courseops import db, importer, styling

FIXTURE = Path(__file__).parent / "fixtures" / "messy_course.kml"


@pytest.fixture
def staged(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m", "Marathon")
    importer.stage_file(conn, event_id, FIXTURE)
    return conn, event_id


def line_ids(conn, event_id):
    return [
        r["id"]
        for r in importer.pending_features(conn, event_id)
        if r["geom_type"] == "linestring"
    ]


# --- validation -------------------------------------------------------------

def test_color_validation():
    assert styling.is_valid_color("#cc3333")
    assert styling.is_valid_color("#C33")
    assert not styling.is_valid_color("red")
    assert not styling.is_valid_color("cc3333")
    assert not styling.is_valid_color(None)
    assert styling.normalize_color("#CC3333") == "#cc3333"


def test_dash_presets_and_raw_values():
    assert styling.normalize_dash("solid") is None
    assert styling.normalize_dash("dotted") == "3,7"
    assert styling.normalize_dash("12,8") == "12,8"
    assert styling.normalize_dash("12, 8") == "12,8"
    assert not styling.is_valid_dash("wiggly")


def test_describe_dash_round_trips_preset_names():
    assert styling.describe_dash(None) == "solid"
    assert styling.describe_dash("3,7") == "dotted"


def test_palette_avoids_colors_already_taken():
    first = styling.next_color([])
    second = styling.next_color([first])
    assert first != second
    assert first in styling.DEFAULT_COLORS


def test_palette_wraps_rather_than_failing():
    """A seventh course is beyond any real event; a duplicate beats a crash."""
    assert styling.next_color(list(styling.DEFAULT_COLORS)) in styling.DEFAULT_COLORS


# --- defaults on import -----------------------------------------------------

def test_courses_get_distinct_colors_and_are_solid_by_default(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)

    importer.assign_course(conn, event_id, [ids[0]], name="Full")
    importer.assign_course(conn, event_id, [ids[1]], name="Half")

    courses = importer.courses_for_event(conn, event_id)
    assert len({c["color"] for c in courses}) == 2
    # Overlap is handled by draw order, so nothing is dashed unless asked for.
    assert all(c["dash_pattern"] is None for c in courses)


def test_new_courses_stack_in_creation_order(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)

    importer.assign_course(conn, event_id, [ids[0]], name="Full")
    importer.assign_course(conn, event_id, [ids[1]], name="Half")

    courses = importer.courses_for_event(conn, event_id)
    assert [c["name"] for c in courses] == ["Full", "Half"]
    assert courses[0]["sort_order"] < courses[1]["sort_order"]


def test_explicit_color_and_dash_are_honored(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)

    course_id, _, _ = importer.assign_course(
        conn, event_id, [ids[0]], name="Half", color="#123456", dash="dotted"
    )
    row = conn.execute("SELECT * FROM course WHERE id = ?", (course_id,)).fetchone()
    assert row["color"] == "#123456"
    assert row["dash_pattern"] == "3,7"


def test_invalid_color_on_import_is_rejected(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    with pytest.raises(ValueError, match="hex color"):
        importer.assign_course(conn, event_id, [ids[0]], name="X", color="red")


# --- restyling and reordering -----------------------------------------------

def test_restyle_changes_only_what_is_given(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    course_id, _, _ = importer.assign_course(
        conn, event_id, [ids[0]], name="Half", color="#123456"
    )

    row = importer.set_course_style(conn, event_id, course_id, color="#abcdef")

    assert row["color"] == "#abcdef"
    assert row["name"] == "Half"          # untouched
    assert row["dash_pattern"] is None    # untouched


def test_reorder_puts_a_course_on_top(staged):
    """Draw order is the control for courses that share road."""
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    full, _, _ = importer.assign_course(conn, event_id, [ids[0]], name="Full")
    half, _, _ = importer.assign_course(conn, event_id, [ids[1]], name="Half")

    # Put the Full on top of the Half.
    importer.set_course_style(conn, event_id, full, sort_order=99)

    assert [c["name"] for c in importer.courses_for_event(conn, event_id)] == [
        "Half", "Full"
    ]


def test_restyle_can_set_and_clear_a_dash(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    course_id, _, _ = importer.assign_course(conn, event_id, [ids[0]], name="Half")

    assert importer.set_course_style(
        conn, event_id, course_id, dash="long"
    )["dash_pattern"] == "12,8"
    assert importer.set_course_style(
        conn, event_id, course_id, dash="solid"
    )["dash_pattern"] is None


def test_restyle_rejects_a_bad_dash(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    course_id, _, _ = importer.assign_course(conn, event_id, [ids[0]], name="Half")
    with pytest.raises(ValueError, match="dash pattern"):
        importer.set_course_style(conn, event_id, course_id, dash="wiggly")


def test_restyle_unknown_course_is_rejected(staged):
    conn, event_id = staged
    with pytest.raises(ValueError, match="No course with id"):
        importer.set_course_style(conn, event_id, 999, color="#cc3333")


def test_restyle_with_nothing_to_change_is_rejected(staged):
    conn, event_id = staged
    ids = line_ids(conn, event_id)
    course_id, _, _ = importer.assign_course(conn, event_id, [ids[0]], name="Half")
    with pytest.raises(ValueError, match="Nothing to change"):
        importer.set_course_style(conn, event_id, course_id)


# --- migration --------------------------------------------------------------

def test_missing_columns_are_added_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS skips an existing table, so columns added
    later would never appear without the migration step."""
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    # Simulate a pre-migration database: the tables exist without the columns.
    conn.execute("CREATE TABLE course (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE poi (id INTEGER PRIMARY KEY, name TEXT)")

    applied = db.init_schema(conn)

    assert "course.dash_pattern" in applied
    assert "poi.what3words" in applied
    assert "dash_pattern" in db._column_names(conn, "course")
    assert "what3words" in db._column_names(conn, "poi")


def test_migration_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    assert db.init_schema(conn) == []   # fresh schema already has everything
    assert db.init_schema(conn) == []
