"""A pickup or course note pin can be moved after it is placed (#214).

Source-text checks: the frontend has no build step and no browser in CI. The
server half (log keeps the old spot, range refused) is in test_incidents.py.
"""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_the_popup_offers_move_only_to_a_role_that_can_report():
    body = JS[JS.index("function incidentPopup"):JS.index("function upsertIncidentMarker")]
    assert "inc-move" in body and "can('incident_report')" in body


def test_a_pin_is_draggable_only_while_it_is_being_moved():
    # A pin nobody armed must not be knocked loose while panning a phone.
    body = JS[JS.index("function upsertIncidentMarker"):JS.index("async function setIncidentStatus")]
    assert "draggable" not in body.replace("dragging", "")
    start = JS[JS.index("function startMovePin"):]
    start = start[:start.index("\n}\n")]
    assert "dragging.enable()" in start
    end = JS[JS.index("function stopMovePin"):]
    assert "dragging.disable()" in end[:end.index("\n}\n")]


def test_the_move_saves_through_edit_incident_and_snaps_back_on_failure():
    body = JS[JS.index("async function commitMovePin"):]
    body = body[:body.index("\n}\n")]
    assert "editIncident(" in body and "lat:" in body and "lon:" in body
    assert "setLatLng(" in body          # back to where the server has it
    assert "dragend" in JS


def test_a_map_tap_commits_the_move_and_a_cancel_is_on_screen():
    handler = JS[JS.index("map.on('click'"):]
    handler = handler[:handler.index("});")]
    assert "movingPin" in handler
    assert 'id="pin-move-cancel"' in HTML
