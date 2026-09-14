# Traceability audit, layer 4: setup routes -> module functions

## Summary
Scope: every route in `src/courseops/web.py` from `/robots.txt` (line 554) through `/api/setup/users/{user_id}/delete` (line 1529), plus `/setup/events/{id}/report` (607) and the `publish_setup_changes` middleware (445), traced into `admin.py`, `users.py`, `importer.py`, `kml.py`, `categories.py`, `leaders.py`, `access.py`, `db.py`, `ingest.py`, `report.py`, `styling.py`, `build.py`, `config.py`, `progress.py`. The setup client (`static/setup.js`) was read where a payload key had to be cross-checked. Three findings were reproduced against a scratch database with `tr4_roster.py` / `tr4_sysexit.py` in the scratchpad (nothing in the repo was touched).
Counts: 1 Critical, 3 High, 5 Medium, 8 Low. Headline: enabling tracking on an event with no roster and no course raises `SystemExit` inside the ingest task, which `_supervise_ingest` does not catch, and the event loop dies; the persisted `ingest_enabled=1` then makes the service crash on every restart.
Key structural fact for section 4: `db.connect` opens SQLite with `isolation_level=None` (db.py:17), so every `conn.commit()` in web.py is a no-op and every statement commits on its own. No module ever issues `BEGIN`. Multi-statement operations are therefore never atomic.

## Findings

### TR4-1: Turning tracking on for an event with no roster and no course crashes the server, and the crash persists across restarts
- Severity: Critical
- File: src/courseops/web.py:1054-1094 (route), web.py:2142-2155 (`_supervise_ingest`), src/courseops/ingest.py:179 and 194-198 (`SystemExit`)
- What: `POST /api/setup/events/{id}/tracking {"enabled": true}` checks only the callsign, persists `ingest_enabled=1`, then `start_ingest` creates a task running `run_ingest`. `run_ingest` raises `SystemExit` (not `Exception`) when the event has no APRS-expecting roster entries, no course and no extra filter, and also from `settings.require_callsign()`. `_supervise_ingest` catches `asyncio.CancelledError` and `Exception` only, so `SystemExit` escapes the task and asyncio re-raises it out of the event loop.
- Evidence: `except Exception as exc:  # noqa: BLE001` (web.py:2148); `raise SystemExit(f"Event {event_slug!r} has no APRS-expecting roster entries...` (ingest.py:195). Reproduced: `tr4_sysexit.py` prints `SystemExit escaped the event loop: Event 'empty' has no APRS-expecting roster entries...` after `await app.state.start_ingest("empty")`; the `asyncio.run` call terminates. uvicorn runs its server the same way (`asyncio.run(serve())`).
- Why it matters: a club officer flips the switch on a freshly created event to "see if it connects" and the whole service exits, taking every role link with it. Because `db.set_ingest_enabled` ran first, the lifespan's `for slug in wanted: await _start_ingest(slug)` (web.py:405-406) recreates the task on the next boot and it dies the same way: a systemd restart loop that only a manual `UPDATE event SET ingest_enabled = 0` fixes. The same path is hit at boot when a deploy's `.env` has lost its callsign while an event is enabled (`require_callsign` raises `SystemExit`).
- Fix: in `_supervise_ingest` catch `BaseException` other than `CancelledError` (or make `run_ingest` raise a domain `IngestError`/`ValueError` instead of `SystemExit`; the CLI can convert to `SystemExit` at its edge). In the route, refuse to enable (400) when `tracked == 0` and `area is None`, mirroring `run_ingest`'s check, so the switch cannot show "on" with nothing behind it.
- Effort: S

### TR4-2: Link revoke and relabel are not scoped to the event: any event admin can revoke any other club's live link by id
- Severity: High
- File: src/courseops/web.py:1360 and 1378 (route), src/courseops/access.py:223-239 (`set_label`, `revoke`)
- What: `setup_link_action` authorises on `event_id` via `require_event_admin`, then calls `access.revoke(conn, int(body.get("token_id")))` and `access.set_label(conn, int(...), label)`. Neither function takes an `event_id`; both `UPDATE access_token ... WHERE id = ?` on the bare id. `token_id` comes from the JSON body and is never checked against `event_id`.
- Evidence: `access.revoke(conn, int(body.get("token_id")))` (web.py:1360); `"UPDATE access_token SET revoked = 1 WHERE id = ?", (token_id,)` (access.py:236-237). Reproduced: `access.revoke(conn, <event 2's token id>)` returns `True` when called in the context of event 1.
- Why it matters: `may_access_event` is documented as the single tenancy boundary, and here it is bypassed by a body field. An event admin of one club (or a compromised admin session) can enumerate small integer ids and revoke NCS's link for another club's race on race morning; the failure on their side is a 404 with no explanation. Relabel is the same hole with lower impact.
- Fix: give `access.revoke` and `access.set_label` an `event_id` parameter and add `AND event_id = ?` to both UPDATEs; have the route pass `event_id` and return 404/400 when `rowcount == 0`. Also wrap `int(body.get("token_id"))` so a missing/non-numeric id is a 400 rather than a 500 (see TR4-11).
- Effort: S

### TR4-3: Editing a roster entry's callsign creates a second roster row instead of moving the first
- Severity: High
- File: src/courseops/admin.py:562-571 (`save_roster_entry`), src/courseops/db.py:641-708 (`change_station_key`)
- What: the setup form always sends `original_station_key` when editing (setup.js:2231). `save_roster_entry` calls `db.change_station_key(old, new)`, which for a bare callsign gaining an SSID, or for a different callsign, deliberately BINDS (`bound_key = new`) and leaves `station_key` as it was. `save_roster_entry` then unconditionally calls `db.upsert_roster_entry(conn, event_id, station_key=new, ...)`, which finds no row under the new key and INSERTs one. The event ends up with two roster rows for one person, and the row returned to the client is the new one.
- Evidence: reproduced with `tr4_roster.py`: editing `WX0MIK` to `WX0MIK-9` leaves `[('WX0MIK', 'WX0MIK-9', 'Aid 1'), ('WX0MIK-9', None, 'Aid 1')]`; editing `WX0MIK-9` to `N0CALL-1` leaves `[('WX0MIK-9', 'N0CALL-1', 'Sweep'), ('N0CALL-1', None, 'Sweep')]`. Only the same-base SSID change (`WX0MIK-9` -> `WX0MIK-7`) renames cleanly.
- Why it matters: the roster is the allowlist and the filter. After the edit the station list shows two "Aid 1" rows, both attributing the same packets (`WX0MIK` via `bound_key`, `WX0MIK-9` directly), the status log splits across them, and the officer who "fixed" the SSID sees a duplicate they did not create. The bind/rename split in `change_station_key` was designed for NCS's live "match this heard station" action, not for the setup edit form, and nothing tests the setup path (no test references `original_station_key`).
- Fix: in `save_roster_entry`, after `change_station_key`, upsert against the key the row actually has (`row["station_key"]` returned by `change_station_key`), not the key the form typed; or, for the setup form specifically, do a true rename (UPDATE station_key, with a uniqueness check) since setup edits happen before the event and there is no status log to preserve. Add a test for each of the three branches.
- Effort: M

### TR4-4: Import assignment reads and marks staged features from ANY event, and files places into layers that do not exist
- Severity: High
- File: src/courseops/importer.py:173-176 (`get_feature`), 268 and 342 (callers), 376-381 (`discard`); src/courseops/admin.py:147-181 (`assign_features`)
- What: `POST /api/setup/events/{id}/assign` authorises on `event_id`, but `importer.get_feature(conn, feature_id)` selects by bare id, and `assign_course`, `assign_poi` and `discard` all use it (or an id-only UPDATE) without `event_id`. A feature id from another event is accepted: its geometry is copied into a course/poi in the caller's event and the other event's `import_feature` row is flipped to `assigned`/`discarded`. Separately, `assign_poi` inserts `poi_type` without `categories.get_poi_category`, so a place can be filed into a layer the event does not have.
- Evidence: `rows = [get_feature(conn, fid) for fid in feature_ids]` (importer.py:268); `"UPDATE import_feature SET status = 'discarded' WHERE id = ?"` (importer.py:378). Reproduced: `assign_features(conn, event1, {"kind":"poi","ids":[<event 2 feature>],"poi_type":"nonexistent_layer"})` returned `{'poi_ids': [1]}`, `poi` now holds `(1, 'nonexistent_layer', 'f')` and event 2's feature reads `assigned`. Contrast `admin.create_poi` (admin.py:304) and `update_poi` (340), which do validate the layer.
- Why it matters: the organizer's course geometry is exactly the third-party data the repo's own history was purged for, and it is readable across tenants here by guessing an integer. The layer gap violates the documented rule "A suggestion must name a layer that exists. Otherwise assignment is refused": the "assign all suggestions" button (setup.js:935-955) posts `suggestion.slice(4)` as `poi_type`, so a hint-derived suggestion such as `finish` or `aid_station` for a club that deleted that default layer files the place into nothing - present in the table, invisible on the map, no error.
- Fix: give `get_feature` and `discard` an `event_id` and add `AND event_id = ?`; in `assign_poi` (or `assign_features`) call `categories.get_poi_category(conn, event_id, poi_type)` before the INSERT so the existing `CategoryError` (a `ValueError`) becomes the 400 the rule promises.
- Effort: S

### TR4-5: Deleting an event leaves its APRS-IS feed running under the dead slug
- Severity: Medium
- File: src/courseops/web.py:789-807 (`setup_delete_event`), admin.py:123-126
- What: the route calls `admin.delete_event` (a cascading DELETE) and never `stop_ingest`. `app.state.ingest_tasks[slug]` keeps the connection open with the old filter; `Membership.refresh` (ingest.py:151) then reads an empty roster so nothing is stored, but the connection, the area filter and the in-memory `app.state.nearby` list stay alive for an event that no longer exists. If the club re-creates the event with the same slug, `_start_ingest` sees the slug "already running" (web.py:2163) and returns, so the switch reads on/running while the task is bound to the deleted `event_id`.
- Evidence: `admin.delete_event(conn, event_id)` with no `await app.state.stop_ingest(...)` (web.py:800-803); `if slug in app.state.ingest_tasks: return` (web.py:2163).
- Why it matters: the documented privacy rule is that the feed is off unless someone turned it on for a live event; a deleted event should not keep a wildcard filter on its volunteers' callsigns until the next restart, and the re-create case is a switch that lies.
- Fix: read the slug before deleting, then `await app.state.stop_ingest(slug)` and `app.state.ingest_errors.pop(slug, None)`; the same applies to `setup_delete_organization`, which cascades events.
- Effort: S

### TR4-6: A place is created even when the request fails validation, and no resync is published for it
- Severity: Medium
- File: src/courseops/admin.py:309-321 (`create_poi`), src/courseops/db.py:17 (autocommit), web.py:445-458 (middleware)
- What: `create_poi` INSERTs the poi and then delegates `what3words`/`label`/`notes`/`course_ids` to `update_poi`, which raises `ValueError` for a malformed What3Words address or an unknown course id. With `isolation_level=None` the INSERT is already committed, so the route returns 400 ("does not look like a What3Words address") while the place exists without the rejected fields. Because the middleware publishes `resync` only when `status_code < 400`, field clients are not told about the new place either.
- Evidence: `cur = conn.execute("INSERT INTO poi ...")` (admin.py:309) precedes `return update_poi(conn, event_id, poi_id, rest)` (321); `if request.method != "POST" or response.status_code >= 400: return response` (web.py:448).
- Why it matters: the officer sees an error, corrects the address and submits again, and now has two "Water Stop C" pins. The same shape exists in `save_roster_entry` (rename committed, then `assign_station_to_poi` 400s on a bad `poi_id`) and in `setup_create_user` (user committed, then `set_events` can 500).
- Fix: validate before the INSERT (run `update_poi`'s checks on `rest` first, or wrap the two steps in `BEGIN ... COMMIT`/`ROLLBACK` for the connection since `isolation_level=None` permits explicit transactions). Longer term, give the route helpers a `with conn:`-style transaction so a 4xx means nothing was written.
- Effort: S

### TR4-7: Every `conn.commit()` in the setup routes is a no-op; multi-step writes are not atomic
- Severity: Medium
- File: src/courseops/db.py:17; web.py:877, 920, 932, 946, 1149, 1166, 1180, 1195, 1213, 1227, 1246, 1263, 1277, 1291, 1310 (the `conn.commit()` calls)
- What: `sqlite3.connect(path, isolation_level=None)` puts the connection in autocommit; `conn.commit()` does nothing and no module issues `BEGIN`. The commit calls give a reader the impression that `reorder_pois`'s loop, `set_poi_courses`' DELETE+INSERTs, `create_event`'s INSERT+seed+tokens and `users.set_password`'s UPDATE+DELETE are one unit; they are not. Half of the routes that write (update_event, assign, update_course, update_poi, save_roster, links, organizations, users) have no commit at all and behave identically, which shows the calls are vestigial.
- Evidence: `conn = sqlite3.connect(path, isolation_level=None)  # autocommit` (db.py:17); `count = _guard(admin.reorder_courses, ...); conn.commit()` (web.py:875-877).
- Why it matters: the validated-first functions (`reorder_*`, `set_poi_courses`, `move_pois`) cannot fail mid-way in practice, so today the visible damage is TR4-6's shape. But the pattern will bite the next multi-statement function whose second statement can fail (an `IntegrityError` on a FK, a `busy_timeout` expiring on a second writer during the event). Two connections write concurrently during an event: the ingest task and every request.
- Fix: either remove the misleading `conn.commit()` calls, or (better) wrap each mutating route in an explicit transaction (`conn.execute("BEGIN")` / `COMMIT` / `ROLLBACK` on exception) inside `_guard` or a context manager, so a 4xx/5xx response means nothing landed.
- Effort: M

### TR4-8: System administrators bypass event existence, so a bad event id returns 500s instead of 404
- Severity: Low
- File: src/courseops/users.py:360-361 (`may_access_event`), web.py:607-615, 779-787, 1338-1351
- What: `may_access_event` returns `True` for a system admin before looking the event up. Routes then assume the row exists: `report.build` raises `KeyError` (report.py:85), `update_event` does `_row(None)` -> `AttributeError` (admin.py:118), `setup_links` does `event["slug"]` on `None` after `ensure_tokens` has tried to INSERT tokens for a non-existent event (FK -> `sqlite3.IntegrityError`).
- Evidence: `if user.is_system_admin: return True` (users.py:360-361); `raise KeyError(event_id)` (report.py:85).
- Why it matters: a stale bookmark to a deleted event's report or setup page produces an unexplained 500 for the host admin. Not reachable by org/event admins (their branch does `organization_of_event`, which returns `None`).
- Fix: in `require_event_admin`, look the event up once and 404 when missing, before `may_access_event`.
- Effort: S

### TR4-9: `users.get_user` raises `AuthError` outside `_guard` in the user update/delete routes
- Severity: Low
- File: src/courseops/web.py:1505 and 1533; src/courseops/users.py:202-206
- What: `target = users.get_user(conn, user_id)` raises `AuthError("No such user.")` for an unknown id; the route's `try/finally` has no `except`, so it is a 500. `_guard` is used for `set_password` two lines later but not here.
- Evidence: `raise AuthError("No such user.")` (users.py:205); `target = users.get_user(conn, user_id)` inside `try: ... finally:` (web.py:1504-1505).
- Why it matters: two admins editing the same list - one deletes, the other saves - and the second sees a blank error.
- Fix: `target = _guard(users.get_user, conn, user_id)` (400) or catch and 404.
- Effort: S

### TR4-10: Event assignment for users is not scoped to the actor's organization or validated
- Severity: Low
- File: src/courseops/web.py:1495-1496 and 1522-1523; users.py:300-306 (`set_events`)
- What: `users.set_events(conn, user_id, [int(i) for i in body["event_ids"]])` inserts whatever ids arrive. An org admin can attach their event admin to another club's event id; a non-existent id raises `sqlite3.IntegrityError` (FK on `user_event.event_id`) and a non-numeric one `ValueError`, both outside `_guard` -> 500. The `int(...)` conversion also runs after `create_user` has committed (TR4-6 shape).
- Evidence: `users.set_events(conn, created.id, [int(i) for i in body.get("event_ids", [])])` (web.py:1495-1496).
- Why it matters: `may_access_event` checks the organization first, so the cross-org row grants nothing; this is a junk row plus a 500 on bad input, not an escalation. Reported so the boundary is not assumed to be enforced here.
- Fix: filter `event_ids` to events whose `organization_id` matches the target user's organization (or the actor's), and validate before `create_user`.
- Effort: S

### TR4-11: Body values converted with `int()`/`.strip()` outside `_guard` turn client mistakes into 500s
- Severity: Low
- File: src/courseops/web.py:775 (`int(organization_id)`), 1360 and 1378 (`int(body.get("token_id"))`), 1333 (`body.get("station_key", "")` then `.strip()` in admin.py:584), 1492 (`int(organization_id)`), 643-651 (`display_name`/`username` non-string -> `.strip()` in users.py:162,191)
- What: these conversions happen while building the argument list, i.e. before `_guard` runs, so `ValueError`/`TypeError`/`AttributeError` are not mapped. `int(None)` for a missing `token_id` is a `TypeError`. A system admin creating an event with an organization id that does not exist gets `sqlite3.IntegrityError` from `db.create_event` (FK), also unmapped.
- Evidence: `event = _guard(admin.create_event, conn, body, int(organization_id))` (web.py:775) - the `int()` is evaluated first.
- Why it matters: the shipped client sends numbers, so this needs a hand-made request; the cost is a blank error rather than a message. Included because `_guard` exists precisely to prevent it.
- Fix: move the conversions into the guarded callee, or extend `_guard` to catch `TypeError` and `sqlite3.IntegrityError` and map them to 400.
- Effort: S

### TR4-12: `update_event` lets blank name/timezone reach NOT NULL columns
- Severity: Low
- File: src/courseops/admin.py:106-109; schema.sql:13,15
- What: `values.append((payload.get(name) or "").strip() or None)` turns a whitespace-only `name` or `timezone` into `NULL`; both columns are `NOT NULL`, so SQLite raises `IntegrityError`, which `_guard` does not catch. The form's `required` attribute stops an empty string but not a single space.
- Evidence: `values.append((payload.get(name) or "").strip() or None)` (admin.py:109); `name TEXT NOT NULL` (schema.sql:13).
- Why it matters: 500 with no message where `create_event` (admin.py:77) gives "An event needs a short name (slug) and a full name."
- Fix: reject blank `name`/`timezone` with a `ValueError` in `update_event`, as `create_event` does.
- Effort: S

### TR4-13: `delete_roster_role` reports success for a key that does not exist
- Severity: Low
- File: src/courseops/categories.py:395-412; web.py:1218-1233
- What: unlike `delete_poi_category` (which calls `get_poi_category`) and `delete_lead_division` (which checks `known`), `delete_roster_role` counts usage and deletes without checking the key exists; the route then returns `{"deleted": key}` for nothing. The three delete routes also disagree on the "in use" status: 409 for layers, 400 for roles and leaders.
- Evidence: no existence check between `seed_roster_roles` and the `DELETE` (categories.py:401-411).
- Why it matters: cosmetic; a stale client row "deletes" and the list reloads unchanged.
- Fix: add the same `known is None -> CategoryError` check; pick one status for "still in use".
- Effort: S

### TR4-14: Upload temp directory is never removed and the connection leaks on an unexpected parse error
- Severity: Low
- File: src/courseops/web.py:821-837
- What: `tempfile.mkdtemp()` creates a directory per upload and only the file inside is unlinked; the directory stays. `conn.close()` runs only on the success path and the `KmlError` path; any other exception from `stage_file` (e.g. `zipfile.BadZipFile` from `read_kmz` on a truncated archive that still passes `is_zipfile`, or an `IntegrityError`) leaks the connection until GC and is a 500.
- Evidence: `tmp = pathlib.Path(tempfile.mkdtemp()) / f"upload{suffix}"` ... `tmp.unlink(missing_ok=True)` (web.py:823-833).
- Why it matters: one empty directory per import is harmless on a VPS and mildly annoying on the club laptop's `%TEMP%`; the leaked connection holds a WAL read lock briefly.
- Fix: use `tempfile.TemporaryDirectory()` as a context manager; put `admin.staged_features` and `conn.close()` in `try/finally`; catch `zipfile.BadZipFile` alongside `KmlError`.
- Effort: S

## Unconfirmed

### TR4-U1: `setup_roster` GET runs one `staffed_keys` query per place
- Severity: Low
- File: src/courseops/web.py:988-991
- What: `categories.staffed_keys(conn, event_id)` is inside the list comprehension's `if`, so it executes once per poi. Reading the code this is clearly N+1; I did not measure it, and with ~50 places it is milliseconds.
- Fix: hoist to a local before the comprehension.
- Effort: S

## Clean
- Admin-session check and `may_access_event` run BEFORE any body parse or db work on every `/api/setup/events/{id}/...` route (`require_event_admin` is the first statement in each); org/user routes use `require_system_admin`/`require_user_manager` first. No route does db work before the check.
- Every module function that takes `event_id` and touches a child row by id constrains on it EXCEPT the ones in TR4-2 (`access.revoke`, `access.set_label`) and TR4-4 (`importer.get_feature`, `importer.discard`). Checked: `update_course`, `delete_course`, `set_course_style`, `set_bib_color`, `update_poi`, `delete_poi`, `move_pois`, `reorder_pois`, `reorder_courses`, `set_poi_courses`, `assign_station_to_poi`, `change_station_key`, `upsert_roster_entry`, `delete_roster_entry`, all `categories.*` writes, `tokens_for_event`, `create_token`.
- `users.may_access_event` checks organization before per-event assignment, as documented; `may_manage_user` refuses cross-org and system-admin targets.
- `CategoryError` subclasses `ValueError`, so every `categories.*` call through `_guard` maps to 400. `users.AuthError` is in `_guard` too. All `importer`/`admin`/`db` validation errors on the guarded routes are `ValueError`.
- Return values: `admin.*` and `users.list_*`/`create_organization`/`update_organization` return plain dicts; routes wrap `sqlite3.Row` from `categories.*` with `dict(row)`; `User.as_dict()` and `ImportSummary` fields are JSON-serialisable; `staged_features` decodes `geojson` before sending; no `Path`, `set`, `datetime` or dataclass reaches `JSONResponse`. `setup_roster` returns `sorted(set)` as a list.
- Key names the client reads (`courses`, `pois`, `roster`, `features`, `links`, `slug`, `ordered`, `moved`, `deleted`, `poi_ids`, `course_id`, `distance_m`, `warnings`, `user`, `first_run`, `version`, `build`) match what the routes produce.
- Payload keys the callees read all have a client sender in `setup.js` (`original_station_key`, `poi_id`, `course_ids`, `poi_ids`, `keys`, `course_ids`, `token_id`, `label`, `role`, `action`, `event_ids`, `organization_id`, `enabled`, `staffed`, `icon`, `color`, `name`, `kind`, `ids`, `poi_type`, `reverse`). No payload key is silently ignored: `update_event` ignores unknown keys but reads every one the form sends; `update_course` reads `bib_color`, `bib_color_name`, `color`, `dash`, `name`, `sort_order`; `update_poi_category` reads `name`, `staffed`, `visible`, `show_labels`, `icon`, `color`, `sort_order`.
- Numeric ids arriving as strings would still match in SQLite (INTEGER affinity applies to the bound text), and the client sends `Number(...)` anyway; the only id handled as a string on purpose is `set_poi_courses`' comma-separated form value, which is parsed.
- `/healthz` returns exactly `status` + `version` and touches nothing else; `/api/setup/session` sends `build` only to a signed-in user; `build.build_id()` swallows `OSError`/`SubprocessError`.
- Tracking: `_tracking_state` raises `ValueError` for an unknown event and is wrapped in `_guard`; the POST refuses to enable without a usable callsign; `_start_ingest` stops other feeds first and is idempotent for the same slug; `access_filter_preview` swallows everything to `""` by design.
- `publish_setup_changes` matches every `POST /api/setup/events/{id}[/...]` (create at `/api/setup/events` is correctly excluded), publishes only on `< 400`, and `HTTPException` responses pass through it, so a 4xx never triggers a resync. The report page and GET routes are untouched.
- First-user route closes permanently once any user exists and handles the double-submit race; login maps `AuthError` to 401; password change re-authenticates and clears sessions; cookie flags come from `request_is_secure`.
- Literal-before-parameterised ordering holds for `courses/reorder`, `pois/reorder`, `pois/move`, `categories/reorder`, `roles/{key}/delete`, `leaders/reorder`.

## Appendix: route -> module call table

Status: OK = args, return and exceptions all verified fine; MISMATCH = a finding above; UNVERIFIED = not fully traceable from the code alone.

| Route | Module call | Args OK? | Return handled? | Exceptions mapped? | Status |
|---|---|---|---|---|---|
| GET /robots.txt | (none) | - | - | - | OK |
| GET /healthz | `db.connect`, raw `SELECT 1` | yes | yes | any -> 503 | OK |
| GET /setup | `users.any_users(conn)` | yes | bool | none raised | OK |
| GET /setup/events/{id}/report | `report.build(conn, event_id:int)` -> `Report` | yes | passed to `report.render` -> str | `KeyError` (missing event, sys admin only) uncaught | MISMATCH (TR4-8) |
| GET /api/setup/session | `users.resolve_session`, `users.any_users`, `build.build_id()` | yes | `User.as_dict()`/bool/str | none raised | OK |
| POST /api/setup/first-user | `users.any_users`; `users.create_user(conn, username, password, ROLE_SYSTEM_ADMIN, display_name)` | yes (positional matches `create_user(conn, username, password, role, display_name=None, organization_id=None)`); non-string display_name -> AttributeError | `User.as_dict()` | `AuthError` -> 400/409 | OK (TR4-11 note) |
| POST /api/setup/login | `users.authenticate(conn, username, password)`; `users.start_session(conn, user.id)` | yes | `User`, token str | `AuthError` -> 401 | OK |
| POST /api/setup/logout | `users.end_session(conn, token)` | yes | None | none | OK |
| POST /api/setup/password | `users.authenticate`; `users.set_password(conn, user.id, new)` | yes | - | `AuthError` -> 400 | OK |
| GET /api/setup/events | `admin.list_events(conn[, org_id])`; `users.events_for(conn, user.id)`; `users.list_organizations(conn)` | yes | list[dict] | none raised | OK |
| POST /api/setup/events | `admin.create_event(conn, body, int(org_id))` -> `db.create_event`, `categories.seed_*`, `access.ensure_tokens` | `int()` outside guard | dict | `ValueError` -> 400; `IntegrityError` (bad org FK) uncaught | MISMATCH (TR4-11) |
| POST /api/setup/events/{id} | `admin.update_event(conn, event_id, body)` | yes | dict; `_row(None)` if event missing | `ValueError` -> 400; `IntegrityError` (blank name/tz) uncaught | MISMATCH (TR4-8, TR4-12) |
| POST /api/setup/events/{id}/delete | `admin.delete_event(conn, event_id)` | yes | None -> `{"deleted"}` | none raised; feed not stopped | MISMATCH (TR4-5) |
| POST /api/setup/events/{id}/import | `importer.stage_file(conn, event_id, Path)` -> `kml.load`, `ensure_layers_for`; `admin.staged_features(conn, event_id)` | yes | `ImportSummary` fields + list[dict] | `KmlError` -> 400; `BadZipFile`/other uncaught; conn leak | MISMATCH (TR4-14) |
| GET /api/setup/events/{id}/staged | `admin.staged_features(conn, event_id)` -> `importer.pending_features` | yes | list[dict] | none | OK |
| POST /api/setup/events/{id}/assign | `admin.assign_features(conn, event_id, body)` -> `importer.assign_course(conn, event_id, ids, name, color=, reverse=, dash=)`, `importer.assign_poi(conn, event_id, fid, poi_type, name=, what3words=)`, `importer.discard(conn, ids)`, `what3words.normalize` | kwargs match signatures | dict | `ValueError` -> 400 | MISMATCH (TR4-4: no event scope on feature ids; no layer check) |
| GET /api/setup/events/{id}/courses | `admin.list_courses(conn, event_id)`; `admin.list_pois(conn, event_id)` -> `categories.poi_categories`, `progress.CourseIndex.for_event`, `labels.for_poi/derive` | yes | list[dict] | none | OK |
| POST .../courses/reorder | `admin.reorder_courses(conn, event_id, list)` | yes | int | `ValueError` -> 400 | OK |
| POST .../courses/{course_id} | `admin.update_course(conn, event_id, course_id, body)` -> `leaders.set_bib_color(conn, event_id, course_id, color, name)`, `importer.set_course_style(conn, event_id, course_id, **{color,dash,name,sort_order})` | kwargs match `set_course_style` signature | dict; `_row(None)` if course not in event AND payload empty | `ValueError` -> 400 | OK (empty-payload 500 is Low, folded into TR4-8 shape) |
| POST .../courses/{course_id}/delete | `admin.delete_course(conn, event_id, course_id)` | yes | None | none | OK |
| POST .../pois | `admin.create_poi(conn, event_id, body)` -> `categories.get_poi_category`, `_coordinate`, `update_poi` | yes | dict | `ValueError` -> 400 but INSERT already committed | MISMATCH (TR4-6) |
| POST .../pois/reorder | `admin.reorder_pois(conn, event_id, list)` | yes | int | `ValueError` -> 400 | OK |
| POST .../pois/move | `admin.move_pois(conn, event_id, list, key:str)` | yes | int | `ValueError`/`CategoryError` -> 400 | OK |
| POST .../pois/{poi_id} | `admin.update_poi(conn, event_id, poi_id, body)` -> `categories.get_poi_category`, `what3words.*`, `labels.*`, `set_poi_courses` | yes | dict | `ValueError` -> 400 | OK |
| POST .../pois/{poi_id}/delete | `admin.delete_poi(conn, event_id, poi_id)` | yes | None | none | OK |
| GET .../roster | `admin.list_roster`; `categories.roster_roles`; `admin.list_pois`; `categories.staffed_keys` (per poi); `db.excluded_station_keys` | yes | list/dict/set->sorted list | none | OK (TR4-U1) |
| GET .../tracking | `_tracking_state` -> `db.tracked_station_keys`, `progress.CourseIndex.for_event(...).area(AREA_MARGIN_M)`, `settings.callsign_problem`, `aprsis.build_filter` | yes | dict | `ValueError` -> 400 | OK |
| POST .../tracking | `_tracking_state`; `db.set_ingest_enabled(conn, slug, bool)`; `app.state.start_ingest/stop_ingest(slug)` -> `run_ingest` | yes | dict | `SystemExit` from `run_ingest` escapes the task | MISMATCH (TR4-1) |
| GET .../categories | `categories.poi_categories/roster_roles/lead_divisions(conn, event_id)` + raw COUNTs | yes | `dict(row)` | none | OK |
| POST .../categories | `categories.add_poi_category(conn, event_id, name, staffed:bool, icon, color)` | positional matches `(conn, event_id, name, staffed=False, icon="pin", color=None)` | `dict(row)` | `CategoryError` -> 400 | OK |
| POST .../categories/reorder | `categories.reorder_poi_categories(conn, event_id, keys)` | yes | int | `CategoryError` -> 400 | OK |
| POST .../categories/{key} | `categories.update_poi_category(conn, event_id, key, body)` | yes | `dict(row)` | `CategoryError`/`ValueError` -> 400 | OK |
| POST .../categories/{key}/delete | `categories.delete_poi_category(conn, event_id, key)` | yes | int in_use -> 409 | `CategoryError` -> 400 | OK |
| POST .../roles | `categories.add_roster_role(conn, event_id, name)` | yes | `dict(row)` | `CategoryError` -> 400 | OK |
| POST .../roles/{key}/delete | `categories.delete_roster_role(conn, event_id, key)` | yes | int in_use -> 400 | no existence check | MISMATCH (TR4-13, Low) |
| POST .../roles/{key} | `categories.rename_roster_role(conn, event_id, key, name)` | yes | `dict(row)` | `CategoryError` -> 400 | OK |
| POST .../leaders | `categories.add_lead_division(conn, event_id, name)` | yes | `dict(row)` | `CategoryError` -> 400 | OK |
| POST .../leaders/reorder | `categories.reorder_lead_divisions(conn, event_id, keys)` | yes | int | `CategoryError` -> 400 | OK |
| POST .../leaders/{key}/delete | `categories.delete_lead_division(conn, event_id, key)` | yes | int in_use -> 400 | `CategoryError` -> 400 | OK |
| POST .../leaders/{key} | `categories.rename_lead_division(conn, event_id, key, name)` | yes | `dict(row)` | `CategoryError` -> 400 | OK |
| POST .../roster | `admin.save_roster_entry(conn, event_id, body)` -> `db.change_station_key`, `db.upsert_roster_entry(conn, event_id, key, label, category=, expects_aprs=, operator_name=)`, `db.assign_station_to_poi(conn, event_id, key, poi_id|None)` | kwargs match | dict | `ValueError` -> 400 (partial write persists) | MISMATCH (TR4-3, TR4-6) |
| POST .../roster/delete | `admin.delete_roster_entry(conn, event_id, station_key:str)` | `None` station_key -> AttributeError | None | none mapped | OK (TR4-11 note) |
| GET .../links | `access.ensure_tokens(conn, event_id)`; `admin.list_links(conn, event_id)` -> `access.tokens_for_event` | yes | dict/list[dict] | none; `None["slug"]` if event missing (sys admin) | OK (TR4-8) |
| POST .../links | `access.revoke(conn, token_id)`; `access.create_token(conn, event_id, role, label)`; `access.set_label(conn, token_id, label)`; `access.tokens_for_event`; `admin.list_links` | `token_id` unscoped; `int(None)` TypeError | list[dict] | role checked in route; `int()` errors uncaught | MISMATCH (TR4-2, TR4-11) |
| GET /api/setup/organizations | `users.list_organizations(conn)` | yes | list[dict] | none | OK |
| POST /api/setup/organizations | `users.create_organization(conn, slug, name, contact)` | yes | dict | `AuthError` -> 400 | OK |
| POST /api/setup/organizations/{id} | `users.update_organization(conn, organization_id, body)` | yes | dict | `AuthError` -> 400 | OK |
| POST /api/setup/organizations/{id}/delete | raw `DELETE FROM organization` (no module call) | - | `{"deleted"}` even if 0 rows | none; feeds of cascaded events not stopped | OK (TR4-5 note) |
| GET /api/setup/users | `users.list_users(conn)`; `users.list_organizations(conn)` | yes | list[dict] | none | OK |
| POST /api/setup/users | `users.create_user(conn, username, password, role, display_name, org_id|None)`; `users.set_events(conn, created.id, [int...])` | `int()` outside guard | `User.as_dict()` | `AuthError` -> 400; `ValueError`/`IntegrityError` from set_events uncaught, after user committed | MISMATCH (TR4-10, TR4-11) |
| POST /api/setup/users/{user_id} | `users.get_user`; `users.may_manage_user`; `users.set_password` (guarded); `users.count_system_admins(conn, user_id)`; `users.set_active`; `users.set_events`; `users.get_user(...).as_dict()` | yes | dict | `get_user` `AuthError` uncaught; `set_events` errors uncaught | MISMATCH (TR4-9, TR4-10) |
| POST /api/setup/users/{user_id}/delete | `users.get_user`; `users.may_manage_user`; `users.count_system_admins`; `users.delete_user` | yes | `{"deleted"}` | `get_user` `AuthError` uncaught | MISMATCH (TR4-9) |
| middleware `publish_setup_changes` | `app.state.hub.publish(int(event_id), {"type": "resync"})` | yes | - | publishes only on `< 400`; partial writes that end in 4xx get no resync | see TR4-6 |
