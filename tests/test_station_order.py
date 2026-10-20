"""The NCS stations list is alphabetical and never re-sorts (#212).

It used to sort by operational status, then radio status, then place order, so
a row jumped whenever NCS pressed a button or a station went quiet, and
somebody looking for "Water Stop Delta" found it had moved under their finger.
(Before that it followed the club's place order, and before THAT the mile,
which interleaved three races: F at 10K mile 1.7 above A at Full mile 2.2. The
place order still rules the lead runner progression, setup, the roster and the
printed sheet - just not this list.) Now one order, by the name on the row; status is a dot and a colour, never a
position. Silent stations are counted in the heading instead, so a silent
failure is still in front of NCS without moving anything.
"""

import json
import shutil
import subprocess

import pytest

from courseops import web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
UTIL_JS = (web.STATIC_DIR / "util.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _render_stations_source() -> str:
    start = APP_JS.index("function renderStations()")
    return APP_JS[start:APP_JS.index("\n}\n", start)]


def test_the_stations_sort_reads_no_status_and_no_position():
    body = _render_stations_source()
    sort = body[body.index(".sort("):body.index("/* Silent is the alarm")]
    for forbidden in ("opStatusOf", "radioStatus", "OP_RANK", "STATUS_RANK",
                      "placeRankOf", "coursePositionOf", "courseStack"):
        assert forbidden not in sort, f"a row would move when {forbidden} changes"
    assert "compareStationNames" in sort


def test_the_old_status_ranks_are_gone():
    assert "const STATUS_RANK" not in APP_JS
    assert "const OP_RANK" not in APP_JS


def test_silent_stations_are_counted_in_the_heading():
    """Order no longer surfaces them, so the count has to."""
    body = _render_stations_source()
    assert "silent" in body[body.index("station-count"):body.index("unit-section")]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("names, expected", [
    # Numbers sort as numbers: "Aid 10" after "Aid 2", not before it.
    (["Aid 10", "Aid 2", "Aid 1"], ["Aid 1", "Aid 2", "Aid 10"]),
    # Case does not matter; Greek letters sort by spelling, which is the point:
    # the club finds a row by its name.
    (["water stop delta", "Water Stop Alpha", "Water Stop Bravo"],
     ["Water Stop Alpha", "Water Stop Bravo", "water stop delta"]),
    # Accented and mixed names still compare, and equal names are a tie.
    (["Zeta", "Éclair", "alpha"], ["alpha", "Éclair", "Zeta"]),
])
def test_compare_station_names_orders_by_the_name_on_the_row(names, expected, tmp_path):
    script = (
        UTIL_JS
        + f"\nconst names = {json.dumps(names)};"
        + "\nconsole.log(JSON.stringify(names.sort(compareStationNames)));"
    )
    # A file, not `-e`: a non-ASCII argument is mangled on a Windows command line.
    path = tmp_path / "order.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run([NODE, str(path)], capture_output=True, check=True,
                         timeout=30).stdout.decode("utf-8")
    assert json.loads(out) == expected
