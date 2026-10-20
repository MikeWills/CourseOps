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
    assert 'id="ev-unit-kinds"' in (STATIC / "setup.html").read_text(encoding="utf-8")
    assert "unit_kinds" in (STATIC / "setup.js").read_text(encoding="utf-8")


def test_a_select_in_a_button_row_matches_the_buttons():
    """Route stops: the stop picker sat at browser-default size beside 42px buttons."""
    css = (STATIC / "setup.css").read_text(encoding="utf-8")
    block = css[css.index(".row > select"):]
    block = block[:block.index("}")]
    assert "min-height: 42px" in block
