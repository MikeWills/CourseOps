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
