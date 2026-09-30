"""The stations list follows the club's place order, not the mile.

Each posted station's mile is measured on whichever race its place snaps
to, so a sort by mile interleaves three races: the 2026 Mankato NCS panel
listed F (10K mile 1.7), A (Full 2.2), B (Full 4.2), I (Half 4.4) - while
setup, the roster and the printed sheet all read A, B, C in the order the
club dragged the Places rows into. `poi.sort_order` wins everywhere else
(`CourseIndex.order_along_course`); the snapshot sends the places in that
order, and this list has to use it.
"""
from __future__ import annotations

from courseops import web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _sort_block() -> str:
    render = APP_JS[APP_JS.index("function renderStations"):]
    return render[:render.index("document.getElementById('station-count')")]


def test_the_club_order_is_kept_from_the_snapshot():
    apply = APP_JS[APP_JS.index("function applyState"):]
    apply = apply[:apply.index("\n}\n")]
    assert "state.placeOrder = new Map(" in apply


def test_posted_stations_sort_by_place_order_before_any_mile():
    block = _sort_block()
    assert "placeRankOf(a)" in block and "placeRankOf(b)" in block
    # The place order is compared before the mile, or it decides nothing.
    assert block.index("placeRankOf(a)") < block.index("distance_along_m")


def test_the_mile_only_orders_stations_on_the_same_race():
    """Two unposted beaconing stations - a sweep on the Full and one on the
    Half - still sort by mile only when the mile is on the same course."""
    block = _sort_block()
    assert "pa.course_id === pb.course_id" in block


def test_across_races_the_races_follow_the_course_stack():
    """Falling back to the label for two races made the comparison
    inconsistent (A<B by mile, B<C by label, C<A by mile), so the order
    depended on which pairs the sort compared. Races rank as every other
    race-grouped list ranks them: courseStack()."""
    block = _sort_block()
    assert "courseStack()" in block
    assert "stackAt.get(pa.course_id) - stackAt.get(pb.course_id)" in block
    restack = APP_JS[APP_JS.index("function setCourseStack"):]
    restack = restack[:restack.index("\n}\n")]
    assert "renderStations()" in restack
