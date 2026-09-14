"""A roster entry may name a bare callsign; the SSID is learned from the air.

Volunteers know their callsign. The SSID belongs to whichever radio or phone
app they bring on the day, so a coordinator collecting SSIDs weeks in advance
collects some wrong ones - and a wrong SSID makes someone silently invisible,
which is the failure this whole area of the code exists to prevent.

The scenario throughout: K0JZP is rostered as a bare callsign for Aid 3, turns
up beaconing K0JZP-9, and also runs a digipeater on K0JZP-7.
"""

import pytest

from courseops import db, ingest


PERSON = "!4408.55N/09359.20W[090/006Out for a walk"
DIGI = "!4408.55N/09359.20W#PHG5130 Digipeater"


def _packet(station_key, body=PERSON):
    return f"{station_key}>APRS,TCPIP*,qAC,X:{body}"


def _event(tmp_path, station_key="K0JZP"):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(conn, event_id, station_key, "Aid 3", "aid_station",
                           operator_name="Alaric")
    return conn, event_id


def _feed(conn, event_id, *lines):
    stats = ingest.IngestStats()
    for line in lines:
        ingest.handle_line(
            conn, event_id, set(db.all_station_keys(conn, event_id)), line, stats,
            base_callsigns=db.rostered_base_callsigns(conn, event_id),
            excluded=db.excluded_station_keys(conn, event_id),
        )
    return stats


def _roster_row(conn, event_id, station_key):
    return conn.execute(
        "SELECT * FROM roster WHERE event_id = ? AND station_key = ?",
        (event_id, station_key),
    ).fetchone()


def test_a_bare_callsign_learns_its_ssid_from_the_first_packet(tmp_path):
    conn, event_id = _event(tmp_path)
    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] is None

    stats = _feed(conn, event_id, _packet("K0JZP-9"))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] == "K0JZP-9"
    assert stats.bound == {"K0JZP-9": "Aid 3"}


def test_the_bare_callsign_is_not_rewritten(tmp_path):
    """What a human typed stays typed, so a wrong bind can be undone."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    assert db.all_station_keys(conn, event_id) == ["K0JZP"]


def test_a_digipeater_is_never_bound(tmp_path):
    """The wildcard filter drags in the operator's own infrastructure. Binding
    an aid station to their home igate would park that person on the map at
    their house all day - confidently, and wrongly."""
    conn, event_id = _event(tmp_path)

    _feed(conn, event_id, _packet("K0JZP-7", DIGI))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] is None


def test_the_digipeater_does_not_block_the_operator(tmp_path):
    """Order matters on race morning: the digipeater has been up all night, so
    its packet almost always arrives first."""
    conn, event_id = _event(tmp_path)

    _feed(conn, event_id, _packet("K0JZP-7", DIGI), _packet("K0JZP-9"))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] == "K0JZP-9"


def test_binding_happens_once_and_does_not_flip(tmp_path):
    """A second SSID must not steal a station that is already attributed, or a
    marker would jump between two radios for the rest of the event."""
    conn, event_id = _event(tmp_path)

    _feed(conn, event_id, _packet("K0JZP-9"), _packet("K0JZP-5"))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] == "K0JZP-9"


def test_an_ssid_already_on_the_roster_is_not_stolen(tmp_path):
    """Two entries under one callsign: a bare one, and an explicit -5 that
    belongs to someone else's assignment. -5 must stay where it was put."""
    conn, event_id = _event(tmp_path)
    db.upsert_roster_entry(conn, event_id, "K0JZP-5", "Sweep 2", "sweep")

    _feed(conn, event_id, _packet("K0JZP-5"))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] is None


def test_unbinding_lets_the_next_ssid_take_it(tmp_path):
    """The undo: bound to a handheld, then they switch to the mobile rig."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    db.unbind_station(conn, event_id, "K0JZP")
    _feed(conn, event_id, _packet("K0JZP-5"))

    assert _roster_row(conn, event_id, "K0JZP")["bound_key"] == "K0JZP-5"


def test_the_bound_ssid_is_what_the_map_joins_on(tmp_path):
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    row = _roster_row(conn, event_id, "K0JZP")
    assert db.tracking_key(row) == "K0JZP-9"


def test_an_explicit_ssid_tracks_under_itself(tmp_path):
    conn, event_id = _event(tmp_path, station_key="K0JZP-9")

    row = _roster_row(conn, event_id, "K0JZP-9")
    assert db.tracking_key(row) == "K0JZP-9"


def test_status_can_be_set_using_the_key_the_map_shows(tmp_path):
    """The client holds the bound SSID, the roster holds the bare callsign.
    A write naming either one has to land on the same row."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    db.set_op_status(conn, event_id, "K0JZP-9", "active", "MW")

    assert _roster_row(conn, event_id, "K0JZP")["op_status"] == "active"


def test_status_history_stays_on_one_key(tmp_path):
    """The log cannot be rebuilt later, so it must not split across two names
    for the same station."""
    conn, event_id = _event(tmp_path)
    db.set_op_status(conn, event_id, "K0JZP", "active", "MW")
    _feed(conn, event_id, _packet("K0JZP-9"))
    db.set_op_status(conn, event_id, "K0JZP-9", "closed", "MW")

    log = db.op_status_log(conn, event_id)
    assert [row["to_status"] for row in log] == ["active", "closed"]
    assert {row["station_key"] for row in log} == {"K0JZP"}


def test_a_bound_ssid_is_not_reported_as_unexpected(tmp_path):
    """It is the answer to that question, not an instance of it."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    assert db.unexpected_ssids(conn, event_id) == []


def test_an_unbound_extra_ssid_is_still_reported(tmp_path):
    """Bound to -9, then -5 shows up too. That is worth telling NCS about."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"), _packet("K0JZP-5"))

    heard = {row["station_key"] for row in db.unexpected_ssids(conn, event_id)}
    assert heard == {"K0JZP-5"}


def test_a_bare_callsign_is_accepted_by_setup(tmp_path):
    from courseops import admin

    conn, event_id = _event(tmp_path, station_key="K0JZP-9")
    row = admin.save_roster_entry(
        conn, event_id, {"station_key": "w1aw", "display_label": "Aid 5"}
    )
    assert row["station_key"] == "W1AW"


@pytest.mark.parametrize("bad", ["", "not a callsign", "K0JZP-9-3", "!!"])
def test_setup_rejects_something_that_is_not_a_callsign(tmp_path, bad):
    from courseops import admin

    conn, event_id = _event(tmp_path)
    with pytest.raises(ValueError):
        admin.save_roster_entry(
            conn, event_id, {"station_key": bad, "display_label": "Aid 5"}
        )


def test_repointing_a_bare_entry_rebinds_rather_than_renaming(tmp_path):
    """NCS pressing "this is really Aid 3" on an unexpected SSID must not
    overwrite the callsign someone typed, nor split the status log."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))
    db.set_op_status(conn, event_id, "K0JZP-9", "active", "MW")

    row = db.change_station_key(conn, event_id, "K0JZP", "K0JZP-5")

    assert row["station_key"] == "K0JZP"
    assert row["bound_key"] == "K0JZP-5"
    assert {r["station_key"] for r in db.op_status_log(conn, event_id)} == {"K0JZP"}


def test_repointing_an_explicit_entry_still_renames(tmp_path):
    """The original behaviour, unchanged: -1 on the roster, -5 on the air."""
    conn, event_id = _event(tmp_path, station_key="K0JZP-1")

    row = db.change_station_key(conn, event_id, "K0JZP-1", "K0JZP-5")

    assert row["station_key"] == "K0JZP-5"


# --- editing the callsign on the setup screen is a RENAME -------------------
#
# Binding is NCS's tool on the live map: it says which HEARD station is this
# person, and leaves what was typed alone so it stays undoable. The setup form
# is the other direction - the human is correcting what they typed - and
# routing it through the bind logic left two roster rows for one person: the
# original, bound to the new key, and a fresh one inserted under it.

def _rows(conn, event_id):
    return [(r["station_key"], r["bound_key"], r["display_label"], r["poi_id"])
            for r in db.roster_for_event(conn, event_id)]


def _edit(conn, event_id, original, new, **extra):
    from courseops import admin
    return admin.save_roster_entry(conn, event_id, {
        "station_key": new, "original_station_key": original,
        "display_label": "Aid 3", "category": "aid_station", **extra,
    })


@pytest.mark.parametrize("original,new", [
    ("K0JZP-1", "K0JZP-7"),   # SSID typo
    ("K0JZP", "K0JZP-9"),     # bare entry given its SSID
    ("K0JZP-1", "N0CALL-1"),  # the wrong person's callsign entirely
])
def test_editing_a_callsign_in_setup_leaves_one_row(tmp_path, original, new):
    conn, event_id = _event(tmp_path, station_key=original)
    conn.execute("INSERT INTO poi (event_id, name, poi_type, lat, lon)"
                 " VALUES (?, 'Aid 3', 'aid_station', 44.1, -93.9)", (event_id,))
    poi_id = conn.execute("SELECT id FROM poi").fetchone()["id"]
    db.assign_station_to_poi(conn, event_id, original, poi_id)

    row = _edit(conn, event_id, original, new)

    assert row["station_key"] == new
    assert _rows(conn, event_id) == [(new, None, "Aid 3", poi_id)]


def test_a_rename_keeps_a_binding_ncs_made(tmp_path):
    """The binding says which radio was HEARD; the rename says what was
    TYPED. Fixing a typo mid-event must not drop the person off the map."""
    conn, event_id = _event(tmp_path, station_key="K0JZP-1")
    db.change_station_key(conn, event_id, "K0JZP-1", "W1AW-5")   # borrowed rig
    assert _roster_row(conn, event_id, "K0JZP-1")["bound_key"] == "W1AW-5"

    _edit(conn, event_id, "K0JZP-1", "K0JZP-7")

    assert _rows(conn, event_id)[0][:2] == ("K0JZP-7", "W1AW-5")


def test_renaming_onto_the_bound_key_clears_the_binding(tmp_path):
    """A bare entry learned -9 from the air and the human then types -9: one
    key, not a row bound to itself."""
    conn, event_id = _event(tmp_path)
    _feed(conn, event_id, _packet("K0JZP-9"))

    _edit(conn, event_id, "K0JZP", "K0JZP-9")

    assert _rows(conn, event_id)[0][:2] == ("K0JZP-9", None)


def test_renaming_onto_another_entry_is_refused(tmp_path):
    """Two rows tracking one SSID is the bug this exists to prevent, so the
    new key may be neither another row's callsign nor its binding."""
    conn, event_id = _event(tmp_path, station_key="K0JZP-1")
    db.upsert_roster_entry(conn, event_id, "W1AW", "Sweep", "sweep")
    _feed(conn, event_id, _packet("W1AW-9"))

    with pytest.raises(ValueError, match="W1AW"):
        _edit(conn, event_id, "K0JZP-1", "W1AW")
    with pytest.raises(ValueError, match="W1AW"):
        _edit(conn, event_id, "K0JZP-1", "W1AW-9")
    assert [r[0] for r in _rows(conn, event_id)] == ["K0JZP-1", "W1AW"]


def test_editing_an_entry_that_does_not_exist_is_refused(tmp_path):
    conn, event_id = _event(tmp_path)
    with pytest.raises(ValueError, match="not on this event's roster"):
        _edit(conn, event_id, "N0CALL-1", "N0CALL-2")
    assert len(_rows(conn, event_id)) == 1
