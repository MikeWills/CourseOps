"""Incidents: runner pickups tracked by bib, with a status workflow.

The workflow is the point. A pin that is only "there" or "gone" cannot express
the thing NCS actually needs to see: this was requested eight minutes ago and
nobody has been dispatched.
"""

from __future__ import annotations

import pytest

from courseops import db, incidents


@pytest.fixture
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    return conn, event_id


def open_one(conn, event_id, **kwargs):
    defaults = dict(lat=44.14, lon=-93.99, bib="1432", by="MW")
    defaults.update(kwargs)
    return incidents.create(conn, event_id, **defaults)


# --- creating ---------------------------------------------------------------

def test_a_new_incident_starts_as_reported(event):
    conn, event_id = event
    row = open_one(conn, event_id)

    assert row["status"] == "reported"
    assert row["bib"] == "1432"
    assert row["reported_by"] == "MW"
    assert row["reported_at"] is not None
    assert row["closed_at"] is None


def test_the_bib_can_be_unknown_at_first_and_filled_in_later(event):
    """It is routinely called in before anyone has read the number."""
    conn, event_id = event
    row = open_one(conn, event_id, bib=None)
    assert row["bib"] is None

    updated = incidents.update(conn, event_id, row["id"], bib="0917", by="MW")
    assert updated["bib"] == "0917"


def test_an_incident_can_be_placed_at_an_aid_station(event):
    conn, event_id = event
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'Aid Alpha', 'aid_station', 44.14, -93.99)",
        (event_id,),
    )
    poi_id = conn.execute("SELECT id FROM poi").fetchone()["id"]

    row = open_one(conn, event_id, poi_id=poi_id)
    assert row["poi_id"] == poi_id


def test_an_unknown_aid_station_is_rejected(event):
    conn, event_id = event
    with pytest.raises(incidents.IncidentError, match="No aid station"):
        open_one(conn, event_id, poi_id=999)


def test_an_out_of_range_position_is_rejected(event):
    conn, event_id = event
    with pytest.raises(incidents.IncidentError, match="out of range"):
        open_one(conn, event_id, lat=91.0)


# --- the workflow -----------------------------------------------------------

def test_the_workflow_runs_forward(event):
    conn, event_id = event
    row = open_one(conn, event_id)

    # "Dropped off" is its own step: picked up means the runner is in the
    # vehicle and still SAG's responsibility, dropped off means delivered.
    for expected in ("en_route", "picked_up", "dropped_off", "closed"):
        assert incidents.next_status(row["status"]) == expected
        row = incidents.set_status(conn, event_id, row["id"], expected, by="MW")
        assert row["status"] == expected

    assert incidents.next_status("closed") is None


def test_closing_records_when(event):
    conn, event_id = event
    row = open_one(conn, event_id)
    closed = incidents.set_status(conn, event_id, row["id"], "closed", by="MW")
    assert closed["closed_at"] is not None


def test_reopening_clears_the_closed_time(event):
    """Closed by mistake happens; the record should not claim it is still shut."""
    conn, event_id = event
    row = open_one(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "closed", by="MW")
    reopened = incidents.set_status(conn, event_id, row["id"], "en_route", by="AB")
    assert reopened["closed_at"] is None


def test_status_at_tracks_the_current_state_not_the_incident(event):
    """"Waiting 8 minutes" is the age of the STATUS, not of the incident."""
    conn, event_id = event
    row = open_one(conn, event_id)
    conn.execute(
        "UPDATE incident SET status_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (row["id"],),
    )

    moved = incidents.set_status(conn, event_id, row["id"], "en_route", by="MW")
    assert moved["status_at"] > "2020-01-01T00:00:00Z"


def test_an_unknown_status_is_rejected(event):
    conn, event_id = event
    row = open_one(conn, event_id)
    with pytest.raises(incidents.IncidentError, match="Unknown status"):
        incidents.set_status(conn, event_id, row["id"], "banana", by="MW")


# --- ordering ---------------------------------------------------------------

def test_unanswered_reports_sort_above_everything(event):
    """The failure this list exists to prevent: a pickup sitting undispatched."""
    conn, event_id = event
    fresh_report = open_one(conn, event_id, bib="111")
    en_route = open_one(conn, event_id, bib="222")
    incidents.set_status(conn, event_id, en_route["id"], "en_route", by="MW")
    done = open_one(conn, event_id, bib="333")
    incidents.set_status(conn, event_id, done["id"], "closed", by="MW")

    order = [r["bib"] for r in incidents.for_event(conn, event_id)]
    assert order == ["111", "222", "333"]


def test_within_a_status_the_longest_waiting_is_first(event):
    conn, event_id = event
    newer = open_one(conn, event_id, bib="new")
    older = open_one(conn, event_id, bib="old")
    conn.execute(
        "UPDATE incident SET status_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (older["id"],),
    )

    order = [r["bib"] for r in incidents.for_event(conn, event_id)]
    assert order == ["old", "new"]


def test_closed_incidents_can_be_hidden(event):
    conn, event_id = event
    row = open_one(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "closed", by="MW")

    assert len(incidents.for_event(conn, event_id)) == 1
    assert incidents.for_event(conn, event_id, include_closed=False) == []


# --- the log ----------------------------------------------------------------

def test_every_change_is_logged_for_handover(event):
    conn, event_id = event
    row = open_one(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "en_route", by="AB")
    incidents.update(conn, event_id, row["id"], note="waiting at mile 9", by="CD")

    entries = incidents.log_for(conn, row["id"])
    assert [e["action"] for e in entries] == ["created", "status", "edited"]
    assert [e["by"] for e in entries] == ["MW", "AB", "CD"]
    assert "reported -> en_route" in entries[1]["detail"]


# --- keeping medical detail out ---------------------------------------------

def test_notes_are_capped_to_stay_operational(event):
    """A short note describes the situation; a long one becomes a medical
    narrative about an identifiable person. The cap is the guardrail."""
    conn, event_id = event
    row = open_one(conn, event_id, note="x" * 500)
    assert len(row["note"]) == incidents.MAX_NOTE_LENGTH


def test_bib_and_operator_fields_are_capped(event):
    conn, event_id = event
    row = open_one(conn, event_id, bib="9" * 50, by="z" * 50)
    assert len(row["bib"]) == incidents.MAX_BIB_LENGTH
    assert len(row["reported_by"]) == incidents.MAX_WHO_LENGTH


# --- scoping ----------------------------------------------------------------

def test_incidents_are_scoped_to_their_event(event):
    conn, event_id = event
    other = db.create_event(conn, "other", "Other")
    row = open_one(conn, event_id)

    with pytest.raises(incidents.IncidentError):
        incidents.get(conn, other, row["id"])
    assert incidents.for_event(conn, other) == []


def test_update_with_nothing_to_change_is_rejected(event):
    conn, event_id = event
    row = open_one(conn, event_id)
    with pytest.raises(incidents.IncidentError, match="Nothing to change"):
        incidents.update(conn, event_id, row["id"], by="MW")


# --- deleting an oops -------------------------------------------------------

def test_deleting_removes_the_incident_and_its_log(event):
    """A pin dropped on the wrong road is noise, not history.

    Left in the queue it makes the waiting count lie about how many people are
    still waiting on us, which is the one number NCS glances at.
    """
    conn, event_id = event
    row = incidents.create(conn, event_id, lat=44.1, lon=-93.9, bib="101")
    incidents.set_status(conn, event_id, row["id"], "en_route")
    assert len(incidents.log_for(conn, row["id"])) >= 2

    gone = incidents.delete(conn, event_id, row["id"])
    assert gone["id"] == row["id"]                 # returned for the broadcast
    assert incidents.for_event(conn, event_id) == []
    # The log is the history OF this incident, not an audit trail outliving it.
    assert incidents.log_for(conn, row["id"]) == []


def test_deleting_a_pickup_drops_the_waiting_count(event):
    conn, event_id = event
    first = incidents.create(conn, event_id, lat=44.1, lon=-93.9)
    incidents.create(conn, event_id, lat=44.2, lon=-93.8)
    assert incidents.waiting_count(conn, event_id) == 2
    incidents.delete(conn, event_id, first["id"])
    assert incidents.waiting_count(conn, event_id) == 1


def test_deleting_a_course_note_leaves_pickups_alone(event):
    conn, event_id = event
    note = incidents.create(conn, event_id, lat=44.1, lon=-93.9, kind="note",
                            note="Confusing turn")
    pickup = incidents.create(conn, event_id, lat=44.2, lon=-93.8)
    incidents.delete(conn, event_id, note["id"])
    remaining = incidents.for_event(conn, event_id)
    assert [r["id"] for r in remaining] == [pickup["id"]]


def test_deleting_something_from_another_event_is_refused(event, tmp_path):
    """Event scoping, like every other lookup: a token for one event must not
    reach into another."""
    conn, event_id = event
    other = db.create_event(conn, "other", "Other")
    row = incidents.create(conn, other, lat=44.1, lon=-93.9)
    with pytest.raises(incidents.IncidentError):
        incidents.delete(conn, event_id, row["id"])
    assert incidents.get(conn, other, row["id"]) is not None
