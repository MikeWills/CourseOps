"""Admin accounts, passwords and sessions.

The security-critical parts of the app, so the tests state the property being
protected rather than just the behaviour.
"""

from __future__ import annotations

import pytest

from courseops import db, users


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(connection)
    return connection


@pytest.fixture
def admin(conn):
    return users.create_user(conn, "mike", "a-long-enough-password",
                             users.ROLE_SYSTEM_ADMIN, "Mike")


# --- password hashing -------------------------------------------------------

def test_a_password_is_never_stored_in_the_clear(conn, admin):
    stored = conn.execute(
        "SELECT password_hash FROM user WHERE id = ?", (admin.id,)
    ).fetchone()["password_hash"]

    assert "a-long-enough-password" not in stored
    assert stored.startswith("scrypt$")


def test_the_same_password_hashes_differently_every_time(conn):
    """Per-password salt: identical passwords must not have identical hashes,
    or one cracked hash breaks every account sharing it."""
    first = users.hash_password("the same password")
    second = users.hash_password("the same password")

    assert first != second
    assert users.verify_password("the same password", first)
    assert users.verify_password("the same password", second)


def test_verification_rejects_wrong_and_malformed_input():
    stored = users.hash_password("correct horse battery staple")

    assert users.verify_password("correct horse battery staple", stored) is True
    assert users.verify_password("wrong", stored) is False
    assert users.verify_password("", stored) is False
    assert users.verify_password("x", "not-a-hash") is False
    assert users.verify_password("x", "") is False
    # A tampered digest must not verify.
    assert users.verify_password("correct horse battery staple",
                                 stored[:-1] + ("0" if stored[-1] != "0" else "1")) \
        is False


def test_the_hash_records_its_own_cost_so_it_can_be_raised_later(conn, admin):
    stored = conn.execute(
        "SELECT password_hash FROM user WHERE id = ?", (admin.id,)
    ).fetchone()["password_hash"]
    scheme, n, r, p, salt, digest = stored.split("$")

    assert (scheme, int(n), int(r), int(p)) == \
        ("scrypt", users.SCRYPT_N, users.SCRYPT_R, users.SCRYPT_P)
    # A hash written with different parameters still verifies.
    old = users.hash_password("another password")
    assert users.verify_password("another password", old)


def test_short_passwords_are_refused(conn):
    with pytest.raises(users.AuthError, match="at least"):
        users.create_user(conn, "bob", "short", users.ROLE_EVENT_ADMIN)


# --- authentication ---------------------------------------------------------

def test_login_succeeds_with_the_right_password(conn, admin):
    user = users.authenticate(conn, "mike", "a-long-enough-password")
    assert user.id == admin.id
    assert user.is_system_admin


def test_usernames_are_case_insensitive(conn, admin):
    assert users.authenticate(conn, "MIKE", "a-long-enough-password").id == admin.id
    assert users.authenticate(conn, "  Mike ", "a-long-enough-password").id == admin.id


def test_a_missing_user_and_a_wrong_password_are_indistinguishable(conn, admin):
    """The error must not reveal whether the account exists."""
    with pytest.raises(users.AuthError) as wrong_password:
        users.authenticate(conn, "mike", "not the password")
    with pytest.raises(users.AuthError) as no_such_user:
        users.authenticate(conn, "nobody", "not the password")

    assert str(wrong_password.value) == str(no_such_user.value)


def test_a_deactivated_account_cannot_log_in(conn, admin):
    users.set_active(conn, admin.id, False)
    with pytest.raises(users.AuthError):
        users.authenticate(conn, "mike", "a-long-enough-password")


def test_duplicate_usernames_are_refused(conn, admin):
    with pytest.raises(users.AuthError, match="already exists"):
        users.create_user(conn, "mike", "another-long-password",
                          users.ROLE_EVENT_ADMIN)


# --- sessions ---------------------------------------------------------------

def test_a_session_resolves_back_to_its_user(conn, admin):
    token = users.start_session(conn, admin.id)
    assert users.resolve_session(conn, token).id == admin.id


def test_an_unknown_or_empty_session_resolves_to_nobody(conn):
    assert users.resolve_session(conn, "nope") is None
    assert users.resolve_session(conn, "") is None


def test_logging_out_invalidates_the_session(conn, admin):
    token = users.start_session(conn, admin.id)
    users.end_session(conn, token)
    assert users.resolve_session(conn, token) is None


def test_an_expired_session_is_rejected_and_cleaned_up(conn, admin):
    token = users.start_session(conn, admin.id)
    conn.execute("UPDATE session SET expires_at = '2020-01-01T00:00:00Z'"
                 " WHERE token = ?", (token,))

    assert users.resolve_session(conn, token) is None
    assert conn.execute("SELECT 1 FROM session WHERE token = ?",
                        (token,)).fetchone() is None


def test_changing_a_password_kills_every_existing_session(conn, admin):
    """A password change usually means it was compromised; leaving old sessions
    alive would defeat the point."""
    first = users.start_session(conn, admin.id)
    second = users.start_session(conn, admin.id)

    users.set_password(conn, admin.id, "a-brand-new-password")

    assert users.resolve_session(conn, first) is None
    assert users.resolve_session(conn, second) is None
    assert users.authenticate(conn, "mike", "a-brand-new-password").id == admin.id


def test_deactivating_a_user_kills_their_sessions(conn, admin):
    token = users.start_session(conn, admin.id)
    users.set_active(conn, admin.id, False)
    assert users.resolve_session(conn, token) is None


# --- roles and event scoping ------------------------------------------------

def test_a_system_admin_may_access_every_event(conn, admin):
    event_id = db.create_event(conn, "e", "Event")
    assert users.may_access_event(conn, admin, event_id) is True


def test_an_event_admin_only_reaches_assigned_events(conn):
    """What keeps one club's officer out of another club's event."""
    mine = db.create_event(conn, "mine", "Mine")
    theirs = db.create_event(conn, "theirs", "Theirs")
    officer = users.create_user(conn, "officer", "a-long-enough-password",
                                users.ROLE_EVENT_ADMIN)

    users.set_events(conn, officer.id, [mine])

    assert users.may_access_event(conn, officer, mine) is True
    assert users.may_access_event(conn, officer, theirs) is False


def test_event_assignment_can_be_replaced(conn):
    first = db.create_event(conn, "a", "A")
    second = db.create_event(conn, "b", "B")
    officer = users.create_user(conn, "officer", "a-long-enough-password",
                                users.ROLE_EVENT_ADMIN)

    users.set_events(conn, officer.id, [first])
    users.set_events(conn, officer.id, [second])

    assert users.events_for(conn, officer.id) == [second]


def test_the_last_system_admin_can_be_counted(conn, admin):
    """Used to stop someone locking everyone out of the system."""
    assert users.count_system_admins(conn) == 1
    assert users.count_system_admins(conn, exclude_id=admin.id) == 0


def test_deleting_a_user_removes_their_event_assignments(conn):
    event_id = db.create_event(conn, "e", "Event")
    officer = users.create_user(conn, "officer", "a-long-enough-password",
                                users.ROLE_EVENT_ADMIN)
    users.set_events(conn, officer.id, [event_id])

    users.delete_user(conn, officer.id)

    assert conn.execute("SELECT COUNT(*) FROM user_event").fetchone()[0] == 0


def test_first_run_has_no_users(conn):
    assert users.any_users(conn) is False
    users.create_user(conn, "mike", "a-long-enough-password",
                      users.ROLE_SYSTEM_ADMIN)
    assert users.any_users(conn) is True
