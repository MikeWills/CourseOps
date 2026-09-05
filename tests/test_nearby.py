"""Stations heard near the course, and matching them to the roster.

The person with the radio is often not the person whose callsign is on the
roster - a borrowed rig, a club tracker, a spouse's mobile. So the feed asks
for a radius around the course as well as the buddy list, and NCS matches
what it hears to who it is. The deal that makes that acceptable: the public
is SEEN, in memory, by NCS only, and nothing is written down until NCS says
who someone is.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from courseops import access, aprsis, db, hub, ingest, progress, web
from courseops.config import Settings

PERSON = "!4408.55N/09359.20W[090/006Out for a walk"


def _packet(station_key, body=PERSON):
    return f"{station_key}>APRS,TCPIP*,qAC,X:{body}"


# --- the filter --------------------------------------------------------------

def test_the_filter_adds_a_radius_around_the_course():
    assert aprsis.build_filter(["N0CALL-9"], area=(44.15, -93.99, 3.2)) \
        == "b/N0CALL* r/44.1500/-93.9900/3"


def test_the_radius_is_at_least_a_kilometre():
    """APRS-IS takes whole kilometres; a tiny course must not round to zero."""
    assert aprsis.build_filter([], area=(44.15, -93.99, 0.2)) == "r/44.1500/-93.9900/1"


def test_the_area_covers_every_course_plus_the_margin(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m) VALUES (?, ?, ?, ?)",
        (event_id, "Half",
         '{"type":"LineString","coordinates":[[-94.00,44.10],[-93.98,44.12]]}', 2800.0),
    )
    lat, lon, radius_m = progress.CourseIndex.for_event(conn, event_id).area(1609.344)
    assert (round(lat, 3), round(lon, 3)) == (44.11, -93.99)
    # Half the box diagonal (~1.4 km) plus a mile.
    assert 2900 < radius_m < 3200


def test_no_course_means_no_area(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    assert progress.CourseIndex.for_event(conn, event_id).area(1000) is None


# --- what the feed does with a stranger --------------------------------------

@pytest.fixture()
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(conn, event_id, "K0JZP", "Aid 3", "aid_station")
    return conn, event_id


def _feed(conn, event_id, line, membership=None, nearby=None):
    m = membership or ingest.Membership(conn, event_id)
    stats = ingest.IngestStats()
    report = ingest.handle_line(
        conn, event_id, m.roster_keys, line, stats,
        base_callsigns=m.base_callsigns, excluded=m.excluded, nearby=nearby,
    )
    return report, stats


def test_a_stranger_is_seen_but_never_stored(event):
    """Not in position, and not in raw_packet either: an area filter delivers
    the public, and the deal is that nothing about them is written down."""
    conn, event_id = event
    nearby = []
    report, stats = _feed(conn, event_id, _packet("W1AW-9"), nearby=nearby)
    assert report is None
    assert [r.station_key for r in nearby] == ["W1AW-9"]
    assert stats.not_rostered == 1
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_packet").fetchone()[0] == 0


def test_a_rostered_station_is_stored_as_before(event):
    conn, event_id = event
    nearby = []
    report, _ = _feed(conn, event_id, _packet("K0JZP-9"), nearby=nearby)
    assert report is not None
    assert nearby == []
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 1


def test_an_empty_roster_stores_nothing(tmp_path):
    """The roster is the allowlist. With nobody on it, an area filter would
    otherwise store everyone in town."""
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    nearby = []
    report, _ = _feed(conn, event_id, _packet("W1AW-9"), nearby=nearby)
    assert report is None
    assert len(nearby) == 1


def test_membership_is_re_read_so_a_match_takes_effect_live(event):
    """NCS matches W1AW-9 to Aid 3 mid-event. From then on its packets must be
    stored, so who-is-known cannot be a snapshot from when the feed started."""
    conn, event_id = event
    membership = ingest.Membership(conn, event_id)
    _, _ = _feed(conn, event_id, _packet("W1AW-9"), membership)
    assert "W1AW-9" not in membership.roster_keys

    db.change_station_key(conn, event_id, "K0JZP", "W1AW-9")
    membership.refresh(force=True)
    assert "W1AW-9" in membership.roster_keys

    report, _ = _feed(conn, event_id, _packet("W1AW-9"), membership)
    assert report is not None
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 1


# --- matching across callsigns ------------------------------------------------

def test_a_roster_entry_can_be_matched_to_a_different_callsign(event):
    """Bound, not renamed: what a human typed survives, the status log stays
    on one key, and it is undoable."""
    conn, event_id = event
    row = db.change_station_key(conn, event_id, "K0JZP", "W1AW-9")
    assert row["station_key"] == "K0JZP"
    assert row["bound_key"] == "W1AW-9"
    assert db.tracking_key(row) == "W1AW-9"


def test_a_station_already_matched_cannot_be_matched_twice(event):
    conn, event_id = event
    db.upsert_roster_entry(conn, event_id, "N0PBA", "Aid 4", "aid_station")
    db.change_station_key(conn, event_id, "K0JZP", "W1AW-9")
    with pytest.raises(ValueError, match="already belongs to K0JZP"):
        db.change_station_key(conn, event_id, "N0PBA", "W1AW-9")


def test_unmatching_puts_the_entry_back_to_waiting(event):
    conn, event_id = event
    db.change_station_key(conn, event_id, "K0JZP", "W1AW-9")
    db.unbind_station(conn, event_id, "K0JZP")
    row = conn.execute("SELECT * FROM roster WHERE station_key = 'K0JZP'").fetchone()
    assert row["bound_key"] is None
    assert db.tracking_key(row) == "K0JZP"


# --- who gets told ----------------------------------------------------------

def test_the_hub_only_delivers_a_gated_message_to_a_role_that_can_act():
    h = hub.Hub()
    ncs = h.subscribe(1, {access.CAP_SSID, access.CAP_INCIDENTS})
    liaison = h.subscribe(1, {access.CAP_INCIDENT_REPORT})
    asyncio.run(h.publish(1, {"type": "nearby"}, requires=access.CAP_SSID))
    asyncio.run(h.publish(1, {"type": "position"}))
    assert [m["type"] for m in _drain(ncs)] == ["nearby", "position"]
    assert [m["type"] for m in _drain(liaison)] == ["position"]


def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


@pytest.fixture()
def app_with_nearby(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    db.upsert_roster_entry(conn, event_id, "K0JZP", "Aid 3", "aid_station")
    tokens = access.ensure_tokens(conn, event_id)
    conn.close()
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=db_path, log_level="WARNING")
    app = web.create_app(settings)
    app.state.nearby[event_id] = {
        "W1AW-9": {"station_key": "W1AW-9", "received_at": "2026-10-17T14:05:00Z",
                   "lat": 44.1, "lon": -94.0, "symbol": "Person", "packets": 3,
                   "looks_like_infrastructure": False, "course_position": None},
    }
    return app, tokens, event_id


def test_only_ncs_sees_the_nearby_list(app_with_nearby):
    app, tokens, _ = app_with_nearby
    with TestClient(app) as client:
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
        for role in ("sag", "liaison", "logistics"):
            other = client.get(f"/api/m2026/{tokens[role]}/state").json()
            assert "nearby" not in other, role
    assert [n["station_key"] for n in ncs["nearby"]] == ["W1AW-9"]


def test_matching_removes_the_station_from_the_list(app_with_nearby):
    app, tokens, event_id = app_with_nearby
    with TestClient(app) as client:
        response = client.post(f"/api/m2026/{tokens['ncs']}/ssid/adopt",
                               json={"from_station_key": "K0JZP",
                                     "to_station_key": "W1AW-9"})
        assert response.status_code == 200
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert ncs["nearby"] == []
    assert next(r for r in ncs["roster"] if r["station_key"] == "K0JZP")["tracking_key"] == "W1AW-9"


def test_ignoring_removes_the_station_from_the_list(app_with_nearby):
    app, tokens, _ = app_with_nearby
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/ssid/ignore",
                    json={"station_key": "W1AW-9"})
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert ncs["nearby"] == []


def test_unmatching_is_ncs_only_and_undoes_a_match(app_with_nearby):
    app, tokens, _ = app_with_nearby
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/ssid/adopt",
                    json={"from_station_key": "K0JZP", "to_station_key": "W1AW-9"})
        refused = client.post(f"/api/m2026/{tokens['sag']}/ssid/unbind",
                              json={"station_key": "K0JZP"})
        assert refused.status_code == 403
        undone = client.post(f"/api/m2026/{tokens['ncs']}/ssid/unbind",
                             json={"station_key": "K0JZP"})
        assert undone.status_code == 200
        assert undone.json()["was"] == "W1AW-9"
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
    assert next(r for r in ncs["roster"] if r["station_key"] == "K0JZP")["tracking_key"] == "K0JZP"


# --- the ignored list: undo for a mis-tap -----------------------------------

def test_ncs_sees_the_ignored_list_and_can_unignore(app_with_nearby):
    """Ignoring is one tap and its effect is silence, so a mistake is
    invisible unless the ignored stations can be seen somewhere."""
    app, tokens, _ = app_with_nearby
    with TestClient(app) as client:
        client.post(f"/api/m2026/{tokens['ncs']}/ssid/ignore",
                    json={"station_key": "W1AW-9", "reason": "Digipeater"})
        ncs = client.get(f"/api/m2026/{tokens['ncs']}/state").json()
        assert [(r["station_key"], r["reason"]) for r in ncs["ignored"]] == [("W1AW-9", "Digipeater")]
        for role in ("sag", "liaison", "logistics"):
            assert "ignored" not in client.get(f"/api/m2026/{tokens[role]}/state").json(), role

        refused = client.post(f"/api/m2026/{tokens['sag']}/ssid/unignore",
                              json={"station_key": "W1AW-9"})
        assert refused.status_code == 403
        undone = client.post(f"/api/m2026/{tokens['ncs']}/ssid/unignore",
                             json={"station_key": "w1aw-9"})
        assert undone.status_code == 200
        assert client.get(f"/api/m2026/{tokens['ncs']}/state").json()["ignored"] == []
        again = client.post(f"/api/m2026/{tokens['ncs']}/ssid/unignore",
                            json={"station_key": "W1AW-9"})
        assert again.status_code == 404


def test_an_ignored_station_is_dropped_and_not_logged(event):
    conn, event_id = event
    db.exclude_station(conn, event_id, "W1AW-9", "Digipeater")
    nearby = []
    report, stats = _feed(conn, event_id, _packet("W1AW-9"), nearby=nearby)
    assert report is None and nearby == []
    assert stats.excluded == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_packet").fetchone()[0] == 0
