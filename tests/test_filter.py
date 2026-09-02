from aprswebtracker.aprsis import BUDDIES_PER_CLAUSE, build_filter, build_login


def test_buddy_filter_for_small_roster():
    assert build_filter(["N0CALL-9", "W1AW-7"]) == "b/N0CALL-9/W1AW-7"


def test_long_roster_splits_across_clauses():
    keys = [f"N0CAL{i:02d}-9" for i in range(45)]
    clauses = build_filter(keys).split(" ")
    assert len(clauses) == 3
    assert all(c.startswith("b/") for c in clauses)
    assert all(len(c.split("/")) - 1 <= BUDDIES_PER_CLAUSE for c in clauses)


def test_extra_filter_is_appended():
    result = build_filter(["N0CALL-9"], "r/34.73/-86.58/30")
    assert result == "b/N0CALL-9 r/34.73/-86.58/30"


def test_login_is_receive_only_by_default():
    login = build_login("N0CALL", "-1", "b/W1AW-9")
    assert login.startswith("user N0CALL pass -1 ")
    assert "filter b/W1AW-9" in login


def test_no_aprs_operator_is_filtered_out_but_still_rostered(tmp_path):
    """expects_aprs=0 means 'do not alert when silent', not 'discard'."""
    from aprswebtracker import db

    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(conn, event_id, "N0CALL-7", "Half-back", "sweep")
    db.upsert_roster_entry(
        conn, event_id, "KI4HMD-1", "Aid 4", "aid_station", expects_aprs=False
    )

    assert db.tracked_station_keys(conn, event_id) == ["N0CALL-7"]
    assert db.all_station_keys(conn, event_id) == ["KI4HMD-1", "N0CALL-7"]
