"""The kinds of unit Liaison and NCS can pick are the club's (#196 follow-up)."""
from __future__ import annotations

from pathlib import Path

import pytest

from courseops import admin, db, snapshot, users

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "k.sqlite3")
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")
    ev = admin.create_event(conn, {"slug": "m", "name": "M"}, org["id"])
    return conn, ev["id"]


def test_an_event_with_none_set_offers_the_usual_three(event):
    conn, eid = event
    assert admin.unit_kinds(conn, eid) == ["Officer", "CSO", "Public Works"]


def test_a_club_sets_its_own_list_one_per_line(event):
    conn, eid = event
    saved = admin.update_event(conn, eid, {"unit_kinds": "Police\n  Fire \n\nPolice\nEMS"})
    assert admin.unit_kinds(conn, eid) == ["Police", "Fire", "EMS"]
    assert saved["unit_kinds"] == "Police\nFire\nEMS"


def test_emptying_it_goes_back_to_the_default(event):
    conn, eid = event
    admin.update_event(conn, eid, {"unit_kinds": "Police"})
    admin.update_event(conn, eid, {"unit_kinds": "  "})
    assert admin.unit_kinds(conn, eid) == ["Officer", "CSO", "Public Works"]


def test_too_many_or_too_long_is_refused(event):
    conn, eid = event
    with pytest.raises(ValueError):
        admin.update_event(conn, eid, {"unit_kinds": "x" * 31})
    with pytest.raises(ValueError):
        admin.update_event(conn, eid, {"unit_kinds": "\n".join(str(i) for i in range(21))})


def test_the_snapshot_carries_them_and_the_page_builds_its_menu_from_them(event):
    conn, eid = event
    admin.update_event(conn, eid, {"unit_kinds": "Police"})
    state = snapshot.build_state(conn, eid)
    assert state["event"]["unit_kinds"] == ["Police"]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "unit_kinds" in js and "<option>Officer</option>" not in html
    assert "unit_kinds" in (STATIC / "setup.js").read_text(encoding="utf-8")


def test_a_select_in_a_button_row_matches_the_buttons():
    """Route stops: the stop picker sat at browser-default size beside 42px buttons."""
    css = (STATIC / "setup.css").read_text(encoding="utf-8")
    block = css[css.index(".row > select"):]
    block = block[:block.index("}")]
    assert "min-height: 42px" in block


def test_unit_types_are_their_own_tab_worked_like_the_roles():
    html = (STATIC / "setup.html").read_text(encoding="utf-8")
    assert 'data-tab="units"' in html and 'data-panel="units"' in html
    panel = html[html.index('data-panel="units"'):html.index("</section>",
                                                            html.index('data-panel="units"'))]
    # A table and a one-at-a-time Add form, like Roles - not a box of lines.
    assert 'id="unit-table"' in panel and 'id="unit-form"' in panel
    assert "<textarea" not in panel
    assert "ev-unit-kinds" not in html


def test_the_setup_default_list_is_the_servers():
    js = (STATIC / "setup.js").read_text(encoding="utf-8")
    line = js[js.index("const DEFAULT_UNIT_KINDS"):].splitlines()[0]
    assert all(f"'{k}'" in line for k in admin.UNIT_KINDS_DEFAULT)


# --- how many units of each type are on the map -------------------------------

def test_a_unit_remembers_its_kind_and_the_setup_counts_them(event):
    conn, eid = event
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    admin.add_unit(conn, eid, {"badge": "4021", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    admin.add_unit(conn, eid, {"badge": "PW12", "kind": "Public Works", "lat": 44.1, "lon": -94.0})
    admin.add_unit(conn, eid, {"badge": "9", "lat": 44.1, "lon": -94.0})      # no kind
    assert admin.unit_kind_counts(conn, eid) == {"Officer": 2, "Public Works": 1}


def test_adding_a_badge_again_with_another_kind_changes_it(event):
    conn, eid = event
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "CSO", "lat": 44.2, "lon": -94.0})
    assert admin.unit_kind_counts(conn, eid) == {"CSO": 1}


def test_units_from_before_the_column_get_their_kind_from_the_label(tmp_path):
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")
    eid = admin.create_event(conn, {"slug": "m", "name": "M"}, org["id"])["id"]
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    conn.execute("UPDATE roster SET unit_kind = NULL")
    conn.execute("ALTER TABLE roster DROP COLUMN unit_kind")
    db.init_schema(conn)
    assert admin.unit_kind_counts(conn, eid) == {"Officer": 1}


def test_the_unit_types_table_shows_the_count():
    js = (STATIC / "setup.js").read_text(encoding="utf-8")
    block = js[js.index("function renderUnitTable"):js.index("$('unit-form')")]
    assert "On the map" in block and "unit_kinds_in_use" in js
