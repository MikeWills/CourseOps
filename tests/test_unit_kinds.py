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


# --- the Partner role -----------------------------------------------------------

def test_a_new_event_has_a_partner_role(event):
    from courseops import categories
    conn, eid = event
    roles = {r["key"]: r["name"] for r in categories.roster_roles(conn, eid)}
    assert roles["partner"] == "Partner"


def test_a_unit_added_on_the_day_is_a_partner_not_a_rover(event):
    conn, eid = event
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    row = conn.execute("SELECT category FROM roster WHERE station_key = '3456'").fetchone()
    assert row["category"] == "partner"


def test_an_event_from_before_gets_the_role_when_its_first_unit_is_added(event):
    from courseops import categories
    conn, eid = event
    categories.roster_roles(conn, eid)                       # seeds the defaults
    conn.execute("DELETE FROM roster_role WHERE event_id = ? AND key = 'partner'", (eid,))
    admin.add_unit(conn, eid, {"badge": "9", "lat": 44.1, "lon": -94.0})
    assert "partner" in {r["key"] for r in categories.roster_roles(conn, eid)}


def test_units_made_before_this_move_from_rover_to_partner_at_startup(tmp_path):
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    org = users.create_organization(conn, "club", "Club")
    eid = admin.create_event(conn, {"slug": "m", "name": "M"}, org["id"])["id"]
    admin.add_unit(conn, eid, {"badge": "3456", "kind": "Officer", "lat": 44.1, "lon": -94.0})
    db.upsert_roster_entry(conn, eid, "N0CALL-7", "Half-back", "rover")
    conn.execute("UPDATE roster SET category = 'rover'")
    conn.execute("DELETE FROM roster_role WHERE key = 'partner'")
    db.init_schema(conn)
    cats = {r["station_key"]: r["category"] for r in conn.execute("SELECT * FROM roster")}
    assert cats == {"3456": "partner", "N0CALL-7": "rover"}   # only units move
    assert conn.execute("SELECT 1 FROM roster_role WHERE key = 'partner'").fetchone()


def test_choosing_hand_placed_in_setup_picks_partner():
    js = (STATIC / "setup.js").read_text(encoding="utf-8")
    block = js[js.index("function renderTrackingHint"):js.index("$('rs-tracking').addEventListener")]
    assert "'partner'" in block
