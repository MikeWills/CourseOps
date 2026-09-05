"""The after-event page for the race lead (issue #7).

One page: how many pickups, over what window, near which stops; and the course
notes with where and when. No names anywhere on it.
"""

import pytest

from courseops import db, incidents, report


@pytest.fixture()
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon",
                               timezone="America/Chicago", event_date="2026-10-17")
    # A straight course running north from 44.10, with two staffed stops on it
    # and one unstaffed mile marker (which must never count as a stop).
    conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m) VALUES (?, ?, ?, ?)",
        (event_id, "Half",
         '{"type":"LineString","coordinates":[[-93.99,44.10],[-93.99,44.20]]}',
         11120.0),
    )
    for name, lat, kind in (("Aid 1", 44.12, "aid_station"),
                            ("Aid 2", 44.17, "aid_station"),
                            ("Mile 3", 44.14, "mile_marker")):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon) VALUES (?, ?, ?, ?, ?)",
            (event_id, name, kind, lat, -93.99),
        )
    return conn, event_id


def _at(conn, incident_id, when):
    conn.execute("UPDATE incident SET reported_at = ? WHERE id = ?", (when, incident_id))


def test_pickups_are_counted_with_a_window_and_by_nearest_stop(event):
    conn, event_id = event
    a = incidents.create(conn, event_id, lat=44.121, lon=-93.99, bib="12")
    b = incidents.create(conn, event_id, lat=44.119, lon=-93.99, bib="13")
    c = incidents.create(conn, event_id, lat=44.171, lon=-93.99)
    d = incidents.create(conn, event_id, lat=44.145, lon=-93.99)   # by the mile marker
    for row, when in ((a, "2026-10-17T14:05:00Z"), (b, "2026-10-17T14:40:00Z"),
                      (c, "2026-10-17T15:30:00Z"), (d, "2026-10-17T16:02:00Z")):
        _at(conn, row["id"], when)

    data = report.build(conn, event_id)

    assert data.pickups == 4
    assert data.first_pickup == "2026-10-17T14:05:00Z"
    assert data.last_pickup == "2026-10-17T16:02:00Z"
    # Ordered along the course, not by name, and the mile marker is not a stop.
    assert [(p.name, p.count) for p in data.by_place] == [("Aid 1", 2), ("Aid 2", 1)]
    assert data.between_stops == 1


def test_a_closed_pickup_still_counts(event):
    """The lead wants how many were called in, not how many are open now."""
    conn, event_id = event
    row = incidents.create(conn, event_id, lat=44.121, lon=-93.99)
    incidents.set_status(conn, event_id, row["id"], "closed")
    assert report.build(conn, event_id).pickups == 1


def test_notes_carry_where_and_when_but_never_who(event):
    conn, event_id = event
    row = incidents.create(conn, event_id, lat=44.1705, lon=-93.99,
                           kind="note", note="Cones down at the corner",
                           by="MW")
    _at(conn, row["id"], "2026-10-17T13:20:00Z")

    data = report.build(conn, event_id)
    [note] = data.notes
    assert note.at == "2026-10-17T13:20:00Z"
    assert note.where.startswith("near Aid 2, mile 4.")
    assert note.text == "Cones down at the corner"
    assert "MW" not in report.render(data)


def test_a_note_is_not_a_pickup_and_a_pickup_is_not_a_note(event):
    conn, event_id = event
    incidents.create(conn, event_id, lat=44.121, lon=-93.99)
    incidents.create(conn, event_id, lat=44.121, lon=-93.99, kind="note", note="x")
    data = report.build(conn, event_id)
    assert data.pickups == 1
    assert len(data.notes) == 1


def test_the_page_renders_times_for_the_browser_to_localise(event):
    """Stored UTC, shown in the event's zone by the browser - the first place
    event.timezone is actually read."""
    conn, event_id = event
    incidents.create(conn, event_id, lat=44.121, lon=-93.99)
    html = report.render(report.build(conn, event_id))
    assert "America/Chicago" in html
    assert '<time datetime="' in html
    assert "1 pickup at" in html


def test_note_text_is_escaped(event):
    conn, event_id = event
    incidents.create(conn, event_id, lat=44.121, lon=-93.99, kind="note",
                     note="<script>alert(1)</script>")
    html = report.render(report.build(conn, event_id))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_an_empty_event_still_renders(event):
    conn, event_id = event
    html = report.render(report.build(conn, event_id))
    assert "No pickups." in html
    assert "Nothing to report." in html


def test_the_time_zone_cannot_break_out_of_the_script(event):
    """The zone is admin-typed text. Inside <script> HTML escaping does not
    protect, so it must never be interpolated there at all."""
    conn, event_id = event
    conn.execute("UPDATE event SET timezone = ? WHERE id = ?",
                 ("</script><script>alert(1)</script>", event_id))
    html = report.render(report.build(conn, event_id))
    assert "<script>alert(1)" not in html
    assert "&lt;/script&gt;" in html
