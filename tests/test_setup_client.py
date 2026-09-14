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
