"""The SAG role, and the split between a pickup and a course note.

SAG drives the course collecting runners who cannot continue. Their question is
not "where is everyone" but "who am I going to get, and has anyone got them
already" - so they get their own link, and enough permission to work that queue
without being able to rewrite the roster or revoke anyone's access.
"""

import pytest

from courseops import access, db, incidents


# --- capabilities -----------------------------------------------------------

def _access(role):
    return access.Access(event_id=1, event_slug="e", role=role, token="t")


def test_sag_may_work_the_pickup_queue():
    assert _access(access.ROLE_SAG).can(access.CAP_INCIDENTS)


@pytest.mark.parametrize("capability", [
    access.CAP_STATIONS, access.CAP_SSID, access.CAP_LEADERS, access.CAP_COURSE,
])
def test_sag_may_do_nothing_else(capability):
    """A bearer link in a moving vehicle. The blast radius of a lost phone
    should be one incident queue, not the whole event."""
    assert not _access(access.ROLE_SAG).can(capability)


def test_ncs_keeps_everything():
    ncs = _access(access.ROLE_NCS)
    assert all(ncs.can(cap) for cap in access.ALL_CAPABILITIES)


@pytest.mark.parametrize("role", [access.ROLE_LIAISON, access.ROLE_LOGISTICS])
def test_the_read_only_roles_stayed_read_only(role):
    granted = _access(role)
    assert not granted.can_write
    assert not any(granted.can(cap) for cap in access.ALL_CAPABILITIES)


def test_every_role_has_a_capability_entry():
    """A role missing from the table would silently have no permissions, which
    is safe but would look like a bug at 6am rather than a policy."""
    assert set(access.ROLE_CAPABILITIES) == set(access.ROLES)


def test_every_role_has_a_label():
    assert set(access.ROLE_LABELS) == set(access.ROLES)


# --- pickups and notes ------------------------------------------------------

@pytest.fixture()
def event(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    return conn, db.create_event(conn, "e", "Event")


def _pickup(conn, event_id, **kw):
    return incidents.create(conn, event_id, lat=44.1, lon=-93.9, **kw)


def test_an_incident_is_a_pickup_unless_it_says_otherwise(event):
    conn, event_id = event
    row = _pickup(conn, event_id)
    assert incidents.Incident(row).as_dict()["kind"] == "pickup"


def test_a_note_records_something_for_the_organizer(event):
    conn, event_id = event
    row = _pickup(conn, event_id, kind="note", note="Intersection unmarshalled")
    data = incidents.Incident(row).as_dict()
    assert data["kind"] == "note"
    assert data["kind_label"] == "Course note"


def test_an_unknown_kind_is_refused(event):
    conn, event_id = event
    with pytest.raises(incidents.IncidentError):
        _pickup(conn, event_id, kind="emergency")


def test_a_note_never_counts_as_someone_waiting(event):
    """The waiting count is read as "who is still out there". A note in it
    would make that number mean nothing."""
    conn, event_id = event
    _pickup(conn, event_id)
    _pickup(conn, event_id, kind="note", note="Cones short at mile 4")

    assert incidents.waiting_count(conn, event_id) == 1


def test_a_delivered_runner_is_no_longer_waiting(event):
    conn, event_id = event
    row = _pickup(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "dropped_off", by="MW")

    assert incidents.waiting_count(conn, event_id) == 0


def test_a_picked_up_runner_is_still_waiting_on_us(event):
    """In the vehicle is not delivered: they are still SAG's responsibility."""
    conn, event_id = event
    row = _pickup(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "picked_up", by="MW")

    assert incidents.waiting_count(conn, event_id) == 1


def test_notes_sort_below_every_pickup(event):
    conn, event_id = event
    _pickup(conn, event_id, kind="note", note="Loose dog at mile 12")
    _pickup(conn, event_id)

    kinds = [r["kind"] for r in incidents.for_event(conn, event_id)]
    assert kinds == ["pickup", "note"]


def test_a_closed_pickup_still_outranks_a_note(event):
    """Notes are not urgent at all, so they sit below even finished pickups
    rather than being interleaved by time."""
    conn, event_id = event
    _pickup(conn, event_id, kind="note", note="Water station short")
    row = _pickup(conn, event_id)
    incidents.set_status(conn, event_id, row["id"], "closed", by="MW")

    assert [r["kind"] for r in incidents.for_event(conn, event_id)] == ["pickup", "note"]


def test_the_drop_off_step_sits_between_picked_up_and_closed(event):
    assert incidents.next_status("picked_up") == "dropped_off"
    assert incidents.next_status("dropped_off") == "closed"
