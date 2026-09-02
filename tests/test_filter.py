from courseops.aprsis import BUDDIES_PER_CLAUSE, build_filter, build_login


def test_the_filter_asks_for_every_ssid_by_default():
    """Wildcard, because a volunteer who signs up as WX0MIK-1 and beacons
    WX0MIK-5 would otherwise be silently invisible on race morning. A missing
    person is far worse than an extra marker."""
    assert build_filter(["N0CALL-9", "W1AW-7"]) == "b/N0CALL*/W1AW*"


def test_exact_matching_is_still_available():
    """For a callsign noisy enough that the wildcard is not worth it."""
    assert build_filter(["N0CALL-9", "W1AW-7"], wildcard=False) ==         "b/N0CALL-9/W1AW-7"


def test_several_ssids_of_one_callsign_collapse_to_one_wildcard():
    assert build_filter(["WX0MIK-1", "WX0MIK-5", "WX0MIK-7"]) == "b/WX0MIK*"


def test_long_roster_splits_across_clauses():
    keys = [f"N0CAL{i:02d}-9" for i in range(45)]
    clauses = build_filter(keys).split(" ")
    assert len(clauses) == 3
    assert all(c.startswith("b/") for c in clauses)
    assert all(len(c.split("/")) - 1 <= BUDDIES_PER_CLAUSE for c in clauses)


def test_extra_filter_is_appended():
    result = build_filter(["N0CALL-9"], "r/34.73/-86.58/30")
    assert result == "b/N0CALL* r/34.73/-86.58/30"


def test_login_is_receive_only_by_default():
    login = build_login("N0CALL", "-1", "b/W1AW-9")
    assert login.startswith("user N0CALL pass -1 ")
    assert "filter b/W1AW-9" in login


def test_no_aprs_operator_is_filtered_out_but_still_rostered(tmp_path):
    """expects_aprs=0 means 'do not alert when silent', not 'discard'."""
    from courseops import db

    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    db.upsert_roster_entry(
        conn, event_id, "KI4HMD-1", "Aid 4", "aid_station", expects_aprs=False
    )

    assert db.tracked_station_keys(conn, event_id) == ["N0CALL-7"]
    assert db.all_station_keys(conn, event_id) == ["KI4HMD-1", "N0CALL-7"]


# --- wildcard delivery and exclusions ---------------------------------------
#
# The wildcard filter is only useful if the ingest side keeps what it delivers.
# The scenario throughout: WX0MIK signed up as -1, actually beacons -5, and also
# runs a digipeater on -7 that must stay off the map.

def _event_with_roster(tmp_path):
    from courseops import db
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(conn, event_id, "WX0MIK-1", "Aid 3", "aid_station")
    return conn, event_id


def _packet(station_key):
    return f"{station_key}>APRS,TCPIP*,qAC,X:!4408.55N/09359.20W>090/006Rolling"


def test_a_wrong_ssid_is_still_tracked(tmp_path):
    """The whole point: the roster says -1, the runner beacons -5."""
    from courseops import db
    from courseops.ingest import IngestStats, handle_line

    conn, event_id = _event_with_roster(tmp_path)
    stats = IngestStats()

    report = handle_line(
        conn, event_id, set(db.all_station_keys(conn, event_id)),
        _packet("WX0MIK-5"), stats,
        base_callsigns=db.rostered_base_callsigns(conn, event_id),
        excluded=db.excluded_station_keys(conn, event_id),
    )

    assert report is not None
    assert stats.stored == 1
    # Surfaced rather than silently absorbed - it is almost always a signup typo.
    assert stats.unexpected_ssid == {"WX0MIK-5"}


def test_an_excluded_ssid_is_dropped(tmp_path):
    """The digipeater. Dismissed once, by name, before the event."""
    from courseops import db
    from courseops.ingest import IngestStats, handle_line

    conn, event_id = _event_with_roster(tmp_path)
    db.exclude_station(conn, event_id, "WX0MIK-7", "digipeater")
    stats = IngestStats()

    report = handle_line(
        conn, event_id, set(db.all_station_keys(conn, event_id)),
        _packet("WX0MIK-7"), stats,
        base_callsigns=db.rostered_base_callsigns(conn, event_id),
        excluded=db.excluded_station_keys(conn, event_id),
    )

    assert report is None
    assert stats.excluded == 1
    assert stats.stored == 0


def test_an_unrelated_callsign_is_still_rejected(tmp_path):
    """Widening to every SSID must not widen to everybody."""
    from courseops import db
    from courseops.ingest import IngestStats, handle_line

    conn, event_id = _event_with_roster(tmp_path)
    stats = IngestStats()

    report = handle_line(
        conn, event_id, set(db.all_station_keys(conn, event_id)),
        _packet("N0BODY-9"), stats,
        base_callsigns=db.rostered_base_callsigns(conn, event_id),
        excluded=db.excluded_station_keys(conn, event_id),
    )

    assert report is None
    assert stats.not_rostered == 1


def test_exclusions_can_be_lifted(tmp_path):
    from courseops import db
    conn, event_id = _event_with_roster(tmp_path)
    db.exclude_station(conn, event_id, "WX0MIK-7", "digipeater")

    assert db.excluded_station_keys(conn, event_id) == {"WX0MIK-7"}
    assert db.unexclude_station(conn, event_id, "WX0MIK-7") is True
    assert db.excluded_station_keys(conn, event_id) == set()
