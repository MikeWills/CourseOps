"""A race's route: the stops its leader passes, in order, repeats allowed (#210).

Mankato, 2026-10-17: the Full goes out to J and back by I, so the leader passes
I twice - H, I, J, I, K. A set of ticked stops and one event-wide order cannot
say that. `course_stop` is the route, one row per step; a race with none is
"not stated" and behaves as it always did. See docs/leader-route.md.
"""

import pytest

from courseops import admin, db, leaders, web


@pytest.fixture()
def race(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Marathon")
    courses = {}
    for name in ("Full", "Half"):
        cur = conn.execute(
            "INSERT INTO course (event_id, name, geojson, distance_m)"
            " VALUES (?, ?, ?, ?)",
            (event_id, name,
             '{"type":"LineString","coordinates":[[-93.99,44.10],[-93.99,44.20]]}',
             11120.0))
        courses[name] = int(cur.lastrowid)
    for i, name in enumerate("HIJK"):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', ?, -93.99)",
            (event_id, name, 44.11 + i * 0.01))
    conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'Mile 3', 'mile_marker', 44.14, -93.99)", (event_id,))
    return conn, event_id, courses


def pid(conn, name):
    return conn.execute("SELECT id FROM poi WHERE name = ?", (name,)).fetchone()["id"]


def names(steps):
    return [s["name"] for s in steps]


def test_a_route_keeps_its_order_and_its_repeats(race):
    conn, event_id, courses = race
    ids = [pid(conn, n) for n in "HIJIK"]

    admin.set_course_route(conn, event_id, courses["Full"], ids)

    steps = admin.course_route(conn, event_id, courses["Full"])
    assert names(steps) == list("HIJIK")
    assert [s["step"] for s in steps] == [0, 1, 2, 3, 4]
    assert steps[1]["poi_id"] == steps[3]["poi_id"] == pid(conn, "I")


def test_saving_again_replaces_the_route(race):
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "HIJ"])
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "KH"])
    assert names(admin.course_route(conn, event_id, courses["Full"])) == ["K", "H"]


def test_an_empty_route_means_not_stated(race):
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Full"], [pid(conn, "H")])
    admin.set_course_route(conn, event_id, courses["Full"], [])
    assert admin.course_route(conn, event_id, courses["Full"]) == []


def test_routes_are_per_race(race):
    """The Half shares some of the Full's stops, skips some, adds others."""
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "HIJIK"])
    admin.set_course_route(conn, event_id, courses["Half"],
                           [pid(conn, n) for n in "HK"])
    assert names(admin.course_route(conn, event_id, courses["Half"])) == ["H", "K"]
    assert names(admin.course_route(conn, event_id, courses["Full"])) == list("HIJIK")


def test_a_place_that_is_not_staffed_cannot_be_a_step(race):
    """A mile marker is not somewhere a leader can be reported."""
    conn, event_id, courses = race
    with pytest.raises(ValueError, match="Mile 3"):
        admin.set_course_route(conn, event_id, courses["Full"], [pid(conn, "Mile 3")])


def test_a_place_from_another_event_cannot_be_a_step(race):
    conn, event_id, courses = race
    other = db.create_event(conn, "other", "Other")
    foreign = int(conn.execute(
        "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
        " VALUES (?, 'X', 'aid_station', 1, 1)", (other,)).lastrowid)
    with pytest.raises(ValueError, match="No place"):
        admin.set_course_route(conn, event_id, courses["Full"], [foreign])


def test_a_course_from_another_event_is_refused(race):
    conn, event_id, courses = race
    other = db.create_event(conn, "other", "Other")
    with pytest.raises(ValueError, match="No course"):
        admin.set_course_route(conn, other, courses["Full"], [pid(conn, "H")])


def test_a_bad_save_leaves_the_old_route_alone(race):
    """One transaction: a refusal half way must not leave half a route."""
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "HI"])
    with pytest.raises(ValueError):
        admin.set_course_route(conn, event_id, courses["Full"],
                               [pid(conn, "K"), pid(conn, "Mile 3")])
    assert names(admin.course_route(conn, event_id, courses["Full"])) == ["H", "I"]


def test_the_draft_is_the_ticked_stops_in_the_clubs_order(race):
    conn, event_id, courses = race
    for n in "KHI":
        admin.set_poi_courses(conn, event_id, pid(conn, n), [courses["Full"]])
    admin.set_poi_courses(conn, event_id, pid(conn, "J"), [courses["Half"]])
    admin.reorder_pois(conn, event_id, [pid(conn, n) for n in "HIJK"])

    draft = admin.draft_course_route(conn, event_id, courses["Full"])

    assert names(draft) == ["H", "I", "K"]
    # A draft is shown for review. It is never saved on its own.
    assert admin.course_route(conn, event_id, courses["Full"]) == []


def test_a_stop_in_use_by_a_route_cannot_be_deleted(race):
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "HIJI"])
    blocked = admin.delete_poi(conn, event_id, pid(conn, "I"))
    assert blocked == "2 route steps"
    assert conn.execute("SELECT COUNT(*) AS c FROM poi WHERE name = 'I'"
                        ).fetchone()["c"] == 1


def test_deleting_a_race_takes_its_route_with_it(race):
    conn, event_id, courses = race
    admin.set_course_route(conn, event_id, courses["Half"], [pid(conn, "H")])
    assert admin.delete_course(conn, event_id, courses["Half"]) is None
    assert conn.execute("SELECT COUNT(*) AS c FROM course_stop").fetchone()["c"] == 0


def test_the_route_travels_with_an_exported_event(race, tmp_path):
    conn, event_id, courses = race
    assert "course_stop" in admin.EXPORTED_TABLES
    admin.set_course_route(conn, event_id, courses["Full"],
                           [pid(conn, n) for n in "HIJIK"])
    out = tmp_path / "out.sqlite3"
    admin.export_event(conn, event_id, out)
    copy = db.connect(out)
    assert copy.execute("SELECT COUNT(*) AS c FROM course_stop").fetchone()["c"] == 5


def test_a_sighting_has_a_nullable_step_and_recording_is_unchanged(race):
    """PR A only adds the column; recording still stores no step."""
    conn, event_id, courses = race
    row = leaders.record_sighting(conn, event_id, courses["Full"], "male",
                                  pid(conn, "H"))
    assert "step" in row.keys()
    assert row["step"] is None


def test_an_older_database_gains_the_table_and_the_column(tmp_path):
    """Both changes are additive: init_schema on a file from before them."""
    conn = db.connect(tmp_path / "old.sqlite3")
    db.init_schema(conn)
    conn.execute("DROP TABLE course_stop")
    # lead_sighting as it was before the column: SQLite cannot drop one from a
    # table whose definition carries comments, so rebuild the old shape.
    conn.execute("DROP TABLE lead_sighting")
    conn.execute(
        "CREATE TABLE lead_sighting ("
        " id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,"
        " course_id INTEGER NOT NULL, division TEXT NOT NULL,"
        " poi_id INTEGER NOT NULL, bib TEXT,"
        " at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
        " by TEXT)")
    db.init_schema(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(lead_sighting)")]
    assert "step" in cols
    conn.execute("SELECT * FROM course_stop").fetchall()


# --- the setup API -----------------------------------------------------------


def _client(tmp_path):
    from fastapi.testclient import TestClient
    from courseops import users
    from courseops.config import Settings
    from courseops.web import create_app

    db_path = tmp_path / "api.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Marathon")
    cur = conn.execute(
        "INSERT INTO course (event_id, name, geojson, distance_m) VALUES (?, 'Full', ?, 1)",
        (event_id, '{"type":"LineString","coordinates":[[-93.99,44.10],[-93.99,44.20]]}'))
    course_id = int(cur.lastrowid)
    for i, name in enumerate("HIJK"):
        conn.execute(
            "INSERT INTO poi (event_id, name, poi_type, lat, lon)"
            " VALUES (?, ?, 'aid_station', ?, -93.99)", (event_id, name, 44.11 + i * 0.01))
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM poi")}
    users.create_user(conn, "mike", "a-long-enough-password", "system_admin")
    conn.close()
    app = create_app(Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                              db_path=db_path, log_level="WARNING"))
    client = TestClient(app)
    client.__enter__()
    client.post("/api/setup/login",
                json={"username": "mike", "password": "a-long-enough-password"})
    return client, event_id, course_id, ids


def test_the_route_is_saved_and_read_back_through_setup(tmp_path):
    client, event_id, course_id, ids = _client(tmp_path)
    url = f"/api/setup/events/{event_id}/courses/{course_id}/route"
    saved = client.post(url, json={"poi_ids": [ids[n] for n in "HIJIK"]})
    assert saved.status_code == 200, saved.text
    got = client.get(url).json()
    assert [s["name"] for s in got["steps"]] == list("HIJIK")


def test_a_refused_route_answers_400_with_the_reason(tmp_path):
    client, event_id, course_id, ids = _client(tmp_path)
    response = client.post(
        f"/api/setup/events/{event_id}/courses/{course_id}/route",
        json={"poi_ids": [999999]})
    assert response.status_code == 400
    assert "No place" in response.json()["detail"]


def test_reading_the_route_never_writes_it(tmp_path):
    """A setup GET must not write: the draft is shown, not saved."""
    client, event_id, course_id, ids = _client(tmp_path)
    url = f"/api/setup/events/{event_id}/courses/{course_id}/route"
    client.get(url)
    assert client.get(url).json()["steps"] == []


def test_the_route_of_an_unknown_course_is_404(tmp_path):
    client, event_id, course_id, ids = _client(tmp_path)
    assert client.get(
        f"/api/setup/events/{event_id}/courses/99999/route").status_code == 404


# --- the setup screen --------------------------------------------------------

SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")
SETUP_HTML = (web.STATIC_DIR / "setup.html").read_text(encoding="utf-8")


def _fn(name):
    start = SETUP_JS.index(f"function {name}(")
    return SETUP_JS[start:SETUP_JS.index("\n}\n", start)]


def test_the_courses_tab_has_a_route_section_with_one_save():
    assert 'id="route-list"' in SETUP_HTML
    assert SETUP_HTML.count('id="route-save-all"') == 1


def test_an_unsaved_route_survives_the_courses_reloading():
    """Saving a course colour reloads the tab; a half-built route must not go."""
    load = SETUP_JS[SETUP_JS.index("async function loadRoutes"):]
    load = load[:load.index("\n}\n")]
    assert "routeDirty(held)" in load


def test_route_text_is_escaped_into_the_markup():
    """Place and race names are club-typed. Each reaches the markup through
    esc(), or through iconBtn, which escapes its attributes and label."""
    import re
    body = _fn("renderRoute")
    body = re.sub(r"const where = [^\n]*\n", "", body)    # a string, not markup
    body = re.sub(r"\$\{esc\([^}]*\)\}", "", body)         # escaped: fine
    body = re.sub(r"iconBtn\([^;]*?\)\}</li>", "", body, flags=re.S)
    for raw in ("${name}", "${cname}", "${where}", "${p.name}"):
        assert raw not in body, f"{raw} reaches the markup unescaped"


def test_nothing_is_saved_by_filling_from_the_draft():
    """The draft is for review: the button only changes the editor's state."""
    body = SETUP_JS[SETUP_JS.index("function bindRouteList"):]
    draft = body[body.index("t.dataset.routedraft)"):body.index("} else { return; }")]
    assert "post(" not in draft
