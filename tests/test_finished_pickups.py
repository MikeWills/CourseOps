"""Finished pickups sit under a folded "Finished (N)" drop-down (#213).

Source-text checks: the frontend has no build step and no browser in CI.
"""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "courseops" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def _render_pickups() -> str:
    body = JS[JS.index("function renderPickups"):]
    return body[:body.index("\n/* Course notes")]


def test_the_split_reuses_incident_done_and_defines_no_second_rule():
    body = _render_pickups()
    assert "incidentDone(" in body
    # one definition only
    assert JS.count("function incidentDone") == 1
    assert "'dropped_off'" not in body and "'closed'" not in body


def test_the_count_still_excludes_finished_rows():
    body = _render_pickups()
    assert "(${live} waiting)" in body


def test_the_finished_group_has_a_44px_toggle_with_count_and_aria():
    assert 'id="finished-toggle"' in HTML
    assert 'aria-controls="finished-list"' in HTML
    assert 'id="finished-list"' in HTML
    # the group sits after the live list
    assert HTML.index('id="incident-list"') < HTML.index('id="finished-toggle"')
    block = CSS[CSS.index(".finished-toggle"):]
    assert "min-height: 44px" in block[:300]
    body = _render_pickups()
    assert "Finished (" in body
    assert "aria-expanded" in body


def test_folded_by_default_and_remembered_in_prefs():
    body = _render_pickups()
    # Stored as OPEN, so an absent entry (every existing device) means folded.
    assert "foldedSections" in body and "'finished-pickups-open'" in JS
    handler = JS[JS.index("getElementById('finished-toggle').addEventListener"):]
    handler = handler[:handler.index("});")]
    assert "savePrefs()" in handler and "renderIncidents()" in handler


def test_a_row_being_edited_is_not_folded_away_mid_edit():
    body = _render_pickups()
    assert "activeElement" in body and "editKey" in body


def test_nothing_waiting_is_not_said_as_no_pickups():
    body = _render_pickups()
    assert "Nothing waiting" in body and "No pickups." in body


def test_rows_are_one_builder_and_notes_are_untouched():
    assert "function buildPickupRow" in JS
    assert "incidentDone" not in JS[JS.index("function renderNotes"):JS.index("async function addEventNote")]
