"""The setup client, checked as text.

The frontend has no build step and no test runner, so what can be checked
here is the shape of the code: that a reader and the markup it reads agree,
and that a fix that was silent when it broke cannot quietly be undone. Each
test names the failure it guards against; none of them exercises a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

from courseops import web

SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    """The text between two anchors, so an assertion is about one handler."""
    begin = SETUP_JS.index(start)
    return SETUP_JS[begin:SETUP_JS.index(end, begin)]


def test_export_csv_reads_the_coordinate_boxes():
    """The Coordinates column shipped empty: the export read a <span> the
    map picker (#108) had replaced with two inputs, and the banner still
    reported success. The reader and the cell must name the same thing."""
    export = _block("$('poi-export').addEventListener", "$('layer-form')")
    assert ".coords span" not in export
    assert "[data-plat]" in export and "[data-plon]" in export
    # The cell those selectors are aimed at still renders both boxes.
    row = _block("<td class=\"coords\">", "</td>")
    assert 'data-plat="${p.id}"' in row and 'data-plon="${p.id}"' in row


def test_a_wrong_password_gets_the_servers_message_not_sign_in_again():
    """api() treats a 401 as an expired session and replaces the body with
    "Sign in again." - which is what every mistyped password was told, and
    showGate() wiped the first-run "Account created" notice on the way. The
    sign-in call is the one 401 that is an answer, and it has to pass."""
    api = _block("async function api(", "const post =")
    assert "path !== LOGIN_PATH" in api
    assert "const data = await post(LOGIN_PATH, body);" in SETUP_JS


def test_the_version_record_is_upgraded_once_a_build_is_known():
    """Signed out, the session carries no build (the commit stays behind the
    login), so a page that loaded on the sign-in form recorded the bare
    version. After sign-in the poll saw a build, the keys differed, and
    "New version - reload" fired on the page that IS the current code - on
    every fresh sign-in on the deployed server. The record taken without a
    build has to give way to the first taken with one, and the poll has to
    offer it before comparing."""
    note = _block("function noteVersion(", "async function noteSignedInVersion")
    assert "hasBuild && !S.loadedVersionHasBuild" in note
    poll = _block("async function pollVersion(",
                  "document.addEventListener('visibilitychange'")
    assert poll.index("noteVersion(data)") < poll.index("checkVersion(data)")
    login = _block("const data = await post(LOGIN_PATH, body);", "await start();")
    assert "noteSignedInVersion()" in login


def test_the_layer_cache_is_dropped_when_the_event_changes():
    """S.poiCategories is fetched lazily and was only ever invalidated by a
    layer reorder, so a host with two events kept event A's layers while
    working on B: every layer dropdown on Import and Places offered A's
    list, and adding a place in a layer B does not have was refused."""
    select = _block("function selectEvent(", "const TIME_ZONES")
    assert "S.poiCategories = null;" in select
    delete = _block("await post(`/api/setup/events/${event.id}/delete`);",
                    "banner(`Deleted ${event.name}.`);")
    assert "S.poiCategories = null;" in delete


def test_every_mutating_handler_reports_a_refusal():
    """api() throws on any non-2xx. A handler that awaits it with no catch
    turns a 400 or 403 into an unhandled rejection: no banner, no reload,
    the row exactly as it was. Revoke on a leaked link, delete on a course,
    remove on a roster entry NCS has rebound - the person could not tell
    "already done" from "refused"."""
    calls = [
        "/courses/${b.dataset.delc}/delete",
        "/pois/${b.dataset.delp}/delete",
        "/roster/delete`",
        "{action: 'add', role: b.dataset.add}",
        "{action: 'label', token_id",
        "{action: 'revoke', token_id",
        "{action: 'reissue', role",
    ]
    for call in calls:
        at = SETUP_JS.index(call)
        # The nearest enclosing async handler must open a try before the call.
        handler = SETUP_JS.rfind("async () =>", 0, at)
        assert "try {" in SETUP_JS[handler:at], call


def test_the_courses_table_saves_as_a_unit():
    """The last editable table with a save button per row, and each press
    reloaded the courses AND the places table under it - the pattern that
    cost a real user twelve renames. bindSaveAll is the one implementation;
    the two bib fields are one setting on the server, so a change to either
    has to carry the other."""
    assert "data-savec" not in SETUP_JS
    courses = _block("bindSaveAll({\n    table: 'course-table'", "noun: 'course(s)'")
    assert "payload.bib_color = " in courses and "payload.bib_color_name = " in courses
    html = (web.STATIC_DIR / "setup.html").read_text(encoding="utf-8")
    assert 'id="course-save-all"' in html and 'id="course-dirty"' in html


def test_container_listeners_are_bound_once_per_table():
    """bindReorder and bindSaveAll listen on the table CONTAINER, whose
    innerHTML is replaced on every load while the element itself persists.
    Nothing removed the previous listeners, so after twenty saves on Places
    one drag posted the order twenty times and broadcast twenty resyncs to
    every phone in the field, and every keystroke ran twenty full-table
    diffs. Invisible until the tab has been used for a while - race week."""
    reorder = _block("function bindReorder(", "/* ---------- organizations")
    assert "if (live.bound) return;" in reorder
    # Every container-level listener sits after the guard.
    guard = reorder.index("if (live.bound) return;")
    assert "tableEl.addEventListener" not in reorder[:guard]
    assert reorder.count("tableEl.addEventListener") == 3
    save_all = _block("function bindSaveAll(", "/* Layers and roles share")
    assert "root.addEventListener('input', () => live.refresh());" in save_all
    assert "if (!live.bound) {" in save_all


def test_the_upload_parses_the_body_before_trusting_it_is_json():
    """A 413 from Apache or a 502 from the proxy is an HTML page, and
    parsing it before checking the status showed "Unexpected token '<'"
    instead of "file too large" - on the KMZ the organizer sent."""
    upload = _block("async function uploadCourseFile(", "async function fillAssignTypes")
    assert "response.json().catch(() => ({}))" in upload
    assert upload.index(".catch(() => ({}))") < upload.index("if (!response.ok)")
