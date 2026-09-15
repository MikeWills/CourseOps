# Code quality and performance audit

## Summary
Scope: every module under `src/courseops/` (26,098 lines total in the repo's Python/JS/HTML/CSS; `web.py` 2184, `app.js` 2787, `setup.js` 2484), with `tests/` and `tools/` read only as callers. Method: AST walk of every top-level def/class/const cross-referenced by regex against the whole repo; the same for every JS function and every CSS class/id; every `CREATE TABLE` column grepped; `build_state` profiled and traced against the demo event (`tools/seed_demo.py`, 3 courses / 900 points / 12 places / 14 roster rows); two failure modes reproduced in Python.
Headline counts: Critical 1 (OPT-1: enabling tracking on an event with no roster and no course exits the server process), High 4, Medium 12, Low 12. Two findings are pure deletions (OPT-5, OPT-6, OPT-7 group) and a third (OPT-8) is close to it.
The APRS parser, geometry, KML/GPX import, incident and leader logic, and both JS files' function inventories are clean: no dead JS function, no unreachable branch found, no mutable default argument, no TODO/FIXME anywhere.

## Findings

### OPT-1: `SystemExit` from the ingest task is not caught and takes the whole server down
- Severity: Critical
- File: src/courseops/web.py:2142-2155 (`_supervise_ingest`), src/courseops/ingest.py:185, src/courseops/ingest.py:195, src/courseops/config.py:74
- What: `run_ingest` signals "cannot start" with `raise SystemExit(...)` in three places (no callsign, unknown slug, "no APRS-expecting roster entries, no course and no extra filter"). `_supervise_ingest` catches `Exception` only; `SystemExit` is a `BaseException`, and asyncio re-raises it out of `run_forever`, so the uvicorn process stops. The tracking switch (web.py:1071-1079) guards the callsign case only, not the empty-roster/no-course case, and the lifespan (web.py:399-406) starts any event with `ingest_enabled = 1` unguarded.
- Evidence: reproduced with the real `run_ingest` against a fresh event with no roster: `await app.state.start_ingest("alpha")` -> `SERVER LOOP KILLED BY SystemExit: Event 'alpha' has no APRS-expecting roster entries, no course and no extra filter.` `app.state.ingest_errors` stays empty. `tests/test_tracking_switch.py:124` covers this path with a `RuntimeError`, which is why it passes.
- Why it matters: a club officer creates an event, flips "Tracking" on before importing the course or adding stations (the switch is on the same screen) and the server exits for every connected phone. The persisted flag also means a deploy restart with a `.env` whose callsign was blanked exits at boot, in a loop under systemd.
- Fix: in `ingest.py` raise a `RuntimeError`/`IngestError` and let `cli.py` translate it to `SystemExit`; in `_supervise_ingest` catch `BaseException` other than `CancelledError`; make the tracking POST refuse (400) when `tracked == 0 and area is None`, the same way it refuses without a callsign. Add the SystemExit case to `test_a_feed_that_dies_records_why`.
- Effort: S

### OPT-2: Every request does synchronous SQLite work on the event loop, including a ~180 ms `/state` build
- Severity: High
- File: src/courseops/web.py:456-460 (`get_conn`), web.py:1616-1652 (`/state`), every `async def` route (86 of them; `grep -c "to_thread\|run_in_threadpool" src/courseops/*.py` = 0)
- What: all 86 routes are `async def` and call `sqlite3` directly. Nothing is offloaded to a thread. `db.connect` costs 6.3 ms per call on this machine (5.3 ms of it is `PRAGMA journal_mode = WAL`, measured), and the NCS `/state` opens three connections (lines 1618, 1645, 1657). `build_state` measured 178 ms on the demo event, 88 % of it in `CourseIndex.locate` (52 calls per snapshot; see OPT-3). With the real Mankato course (1258 points after dedupe, tests/test_exported_course.py:76, three routes sharing it) and ~48 places it scales to seconds.
- Evidence: `cProfile` over 5 `build_state` calls: 1.198 s total, `project_onto_line` 1.276 s cumulative, 780 calls. `db.connect` timed at 6.3 ms; `sqlite3.connect` alone 0.48 ms.
- Why it matters: while one phone's `/state` is being built nothing else moves: WebSocket sends, the ingest loop, other phones' requests. A setup save publishes `resync` to every phone (OPT-4), so twelve phones each fetching `/state` serialise into 12 x 180 ms (or 12 x several seconds on the real course) during which positions stop flowing. `busy_timeout = 5000` makes it worse: a write that waits for the lock waits inside the event loop.
- Fix: (1) keep one connection per request but move the work off the loop: either make the DB-bound routes plain `def` (Starlette runs them in the threadpool; body parsing via a Pydantic model or `Body`) or wrap `build_state(...)` and the other heavy calls in `await asyncio.to_thread(...)`. (2) Set WAL once at `init_schema` (it is persistent in the file) and drop the per-connection pragma, saving 5 ms per request. (3) Reuse the first connection in `/state` instead of opening two more.
- Effort: M (to_thread on `/state`, report, import, and the setup GETs) to L (all routes)

### OPT-3: Each place is projected onto every course four times per snapshot; `locate` has no memo
- Severity: High
- File: src/courseops/web.py:256-263 (`order_along_course` then `_course_position` per poi), src/courseops/leaders.py:311-333 (`index.locate` per staffed place, then `order_along_course` again at 333), src/courseops/progress.py:156-163 (sort key calls `locate` per row)
- What: `CourseIndex.locate` is O(total course vertices) pure-Python geometry (2.0 ms per call on 900 points, measured). `build_state` calls it for every poi in the sort key, again for `course_position`, and `leaders.for_event` repeats both passes on the staffed subset. Positions and incidents are each located once, which is right.
- Evidence: profile shows 52 `locate` calls per `build_state` for 12 places + 6 positions + 4 incidents; `order_along_course` alone is 0.485 s of 1.198 s.
- Why it matters: this is the bulk of OPT-2's cost, and it grows as places x vertices. The organizer's file has 48 mile markers on top of the aid stations.
- Fix: memoise `locate` per `(lat, lon)` inside `CourseIndex` (the index is built per request, so a dict on the instance is safe and needs no invalidation); a 4x reduction for a few lines. Second step, M effort: precompute the planar segment coordinates per course once (using the course centroid's metres-per-degree, accurate to well under a metre over a marathon) so `project_onto_line` stops recomputing `ax, ay, bx, by, dx, dy, seg_sq` for every vertex on every call.
- Effort: S for the memo; M for the precompute

### OPT-4: Every read of the layer or leader list issues `INSERT OR IGNORE` writes, including inside `/state`
- Severity: High
- File: src/courseops/categories.py:129-146 (`seed_poi_categories`), categories.py:465-478 (`seed_lead_divisions`), called from `poi_categories` (categories.py:150) and `lead_divisions` (categories.py:483), which `build_state` calls at web.py:310, 356 and via `leaders.for_event` at leaders.py:284
- What: the "unlike the defaults, this runs every time" loops execute one `INSERT OR IGNORE` per distinct `poi_type` and per distinct `division` on every read. An `INSERT OR IGNORE` that ignores still opens a write transaction and takes the WAL write lock.
- Evidence: SQL trace of one `build_state` on the demo event: 43 statements, 9 of them `INSERT OR IGNORE INTO`. Proven contention: with another connection holding `BEGIN IMMEDIATE`, `INSERT OR IGNORE` of an already-present row raised `database is locked` after the 300 ms busy timeout, while a plain `SELECT` returned in 0.15 ms.
- Why it matters: every phone's snapshot competes for the single writer lock with the ingest task and with each other; with `busy_timeout = 5000` a snapshot can stall the event loop (OPT-2) for up to 5 s waiting on a lock it does not need. Setup GETs (`/categories`, `/courses`) do the same.
- Fix: do the "adopt orphan keys" repair in `init_schema` / `_apply_migrations` and after the writes that can create an orphan (`move_pois`, `assign_poi`, `record_sighting`), not on read. If a read-time safety net is wanted, `SELECT` the missing keys first and insert only when the set is non-empty, which is the common case never.
- Effort: S

### OPT-5: Dead production code (pure deletions)
- Severity: Low
- File: see list
- What: referenced nowhere outside their own definition (grep of `src`, `tests`, `tools`, `docs`, `deploy`):
  - `src/courseops/access.py:103` `WRITE_ROLES` (mentioned only in docs)
  - `src/courseops/access.py:146-183` `ensure_admin_token`, `resolve_admin`, `rotate_admin_token`, and with them the `admin_token` table (`schema.sql:467-474`); superseded by `users.py` sessions. Not in `_ADDED_COLUMNS`, so dropping the table is a `DROP TABLE IF EXISTS` in a migration or simply leaving the DDL out for new databases.
  - `src/courseops/db.py:191` `active_events`
  - `src/courseops/discovery.py:147` `roster_keys_for_event`
  - `src/courseops/leaders.py:33` `DIVISIONS` ("remain only as the fallback for a caller with no database handy" - no such caller exists)
  - `src/courseops/units.py:30` `miles_to_meters`, `units.py:53` `format_mile`
  - `src/courseops/users.py:454` `purge_expired_sessions` (see OPT-14: sessions are never purged)
  - unused imports: `src/courseops/admin.py:22` `geo`; `src/courseops/web.py:21` `Form`
  - unused locals: `src/courseops/cli.py:582` `token` in `cmd_links`; `user` is unpacked and unused in 38 setup routes in web.py (e.g. 609, 781, 813 ...), a sign `require_event_admin` should return just the connection or the routes should be `_`.
  - `src/courseops/static/setup.css:322` `.swatch-dot` (nothing in setup.html/setup.js/icons.js produces it)
  - `src/courseops/ingest.py:55` `log_all_raw` parameter: no caller passes `False`.
- Evidence: reference counts from the AST scan, e.g. `WRITE_ROLES prod=1 tests=0`, `rotate_admin_token prod=1`, `admin_token` appears only in access.py and schema.sql.
- Why it matters: `admin_token` in particular reads like a live second credential path to anyone auditing access; it is not.
- Fix: delete. Pure deletion; the test suite does not touch any of them.
- Effort: S

### OPT-6: Code alive only because tests call it
- Severity: Low
- File: src/courseops/build.py:54 `version_string` (tests/test_build.py:38,65); src/courseops/categories.py:490 `lead_division_keys` (tests/test_leaders.py); src/courseops/hub.py:62 `Hub.subscriber_count` (tests/test_hub.py, test_web.py); src/courseops/importer.py:384 `suggest_event_center` (tests/test_importer.py:140)
- What: no production caller. `suggest_event_center` is the odd one: the setup UI never centres a new event on its imported course, which is what it was written for.
- Evidence: `version_string prod=1 tests=2`, `lead_division_keys prod=1 tests=4`, `subscriber_count prod=1 tests=3`, `suggest_event_center prod=1 tests=1`.
- Why it matters: cleanliness; `subscriber_count` is a reasonable test seam and can stay.
- Fix: delete `version_string` and `lead_division_keys` with their tests, or wire `suggest_event_center` into `assign_features` when the event has no centre. Pure deletion for the first two.
- Effort: S

### OPT-7: `raw_packet` is write-only, unbounded, and stores every line twice
- Severity: Medium
- File: src/courseops/db.py:365-378 (`log_raw_packet`), src/courseops/ingest.py:75-78 and 118-119, src/courseops/schema.sql:237-247; `position.raw` at db.py:355
- What: nothing reads `raw_packet` (`grep -rn "FROM raw_packet" src tools` = nothing; only two test assertions that it is empty). Every stored packet is inserted into `position` with its `raw` column AND into `raw_packet`; every parse failure is inserted too. The schema comment cites "post-event replay" (Phase 7, dropped) and "new parser test fixtures" (captured traffic is gitignored and pasted by hand).
- Evidence: `log_raw_packet` has two callers, both in `handle_line`; no SELECT anywhere.
- Why it matters: two extra autocommit write transactions per packet on the same connection the ingest loop blocks on; a table that grows for the life of the database and is backed up nightly; and a privacy wrinkle for the security audit: ingest.py:75 logs the raw line of an unparseable packet BEFORE the roster check, so with the area filter on, malformed packets from the public are written to disk, contrary to the "not even raw" rule at ingest.py:62-64.
- Fix: drop `log_raw_packet`, `raw_packet` and `log_all_raw` (position.raw keeps the stored packets); or, if the fixture-harvest use is wanted, move the parse-error log after the membership check and cap it.
- Effort: S (deletion)

### OPT-8: One setup row saved = one `resync` = every phone rebuilds the whole map; N rows = N rebuilds
- Severity: Medium
- File: src/courseops/web.py:445-455 (middleware), src/courseops/static/setup.js:1496-1498 (`bindSaveAll` posts one request per changed row), src/courseops/static/app.js:2517-2520 (`resync` -> `loadState()`, no debounce, no in-flight guard), app.js:2480-2485 (`drawCourses`/`drawPois`/all markers torn down and recreated)
- What: the middleware publishes on every 2xx POST under `/api/setup/events/{id}/`, including ones that change nothing on the map (`/tracking`, `/links` label edits). The client answers each with a full `/state` fetch and a full Leaflet rebuild (`removeLayer` every course polyline, poi marker and station marker, then re-add). Twelve renames saved at once are twelve snapshots per phone in a burst.
- Evidence: `bindSaveAll` loop: `for (const [key, payload] of changed) { await save(key, payload); }`; app.js `if (message.type === 'resync') { loadState(); return; }`.
- Why it matters: multiplies OPT-2 and OPT-3 by the number of rows saved times the number of phones, on the one day setup edits happen live.
- Fix: client side, coalesce: on `resync` set a flag and run `loadState` at most once per ~500 ms, and ignore a `resync` that arrives while a fetch is in flight (refetch once after). Server side, exclude `/tracking` and `/links` from the middleware's pattern, and coalesce publishes per event with a short `asyncio` delay so a burst of saves sends one `resync`.
- Effort: S

### OPT-9: A subscriber that overflowed its queue never learns what it missed
- Severity: Medium
- File: src/courseops/hub.py:72-84 (`publish` drops on `QueueFull`), hub.py:8-13 (docstring premise), src/courseops/static/app.js:2570-2580 (`scheduleReconnect` only on socket `close`), app.js:2779-2781 (`visibilitychange` only calls `restoreViewport`)
- What: the hub docstring says a dropped update is harmless because "the client resyncs the full state on reconnect, and the next position report supersedes the lost one". Neither holds for `incident` (a deleted pickup stays on the phone forever), `station_status`, `leaders` or a dropped `resync` itself, and a phone that stalled without the TCP connection closing never reconnects. Nothing re-fetches state on return to the foreground either.
- Evidence: `except asyncio.QueueFull: sub.dropped += 1` then only logging; `QUEUE_MAXSIZE = 64`.
- Why it matters: after a stall the connection badge reads "Live" while the pickup list is wrong, which is exactly the state the badge exists to rule out.
- Fix: on the first drop, clear the queue and enqueue one `{"type": "resync"}` (put_nowait cannot fail on an emptied queue); reset `dropped` when the client catches up. Client side, call `loadState()` on `visibilitychange` to visible when the last message is older than a minute.
- Effort: S

### OPT-10: Per-request write to `access_token.last_used` on every field request
- Severity: Low
- File: src/courseops/access.py:265-269, called from `require_access` (web.py:462), i.e. every `/state`, page, manifest and mutation, and the WebSocket open (web.py:2064)
- What: `resolve` issues an autocommit `UPDATE` per request. It is the same lock as OPT-4 and it is per request, not per session.
- Evidence: `conn.execute("UPDATE access_token SET last_used = ... WHERE token = ?")` unconditionally after the SELECT.
- Why it matters: small on its own (0.14 ms measured) but it is another writer competing with the ingest loop on every phone poll; `last_used` is only ever shown in the Links tab (setup.js:2291) and the CLI.
- Fix: write only when the stored `last_used` is older than a minute (`WHERE token = ? AND (last_used IS NULL OR last_used < ?)`), or update from the WebSocket open only.
- Effort: S

### OPT-11: Four copies of the same reorder routine
- Severity: Low
- File: src/courseops/admin.py:445-475 `reorder_pois`; admin.py:478-501 `reorder_courses`; src/courseops/categories.py:264-290 `reorder_poi_categories`; categories.py:587-613 `reorder_lead_divisions`
- What: each reads the id/key set for the event, checks the request lists exactly that set, then issues one `UPDATE ... sort_order = position*10` per row. The only real differences are the table, the key column, whether a subset is allowed, and the direction (`reversed` for courses).
- Evidence: the four loops are textually identical apart from the table and column names.
- Why it matters: four places to fix the next ordering bug; none of the four runs inside a transaction, so a failure half way leaves a partial order that "looks like it worked" - the thing `reorder_pois`'s own docstring says it refuses to do.
- Fix: one `db.reorder(conn, table, key_column, event_id, keys, *, allow_subset, top_first)` wrapped in `BEGIN`/`COMMIT`; the two `CategoryError`/`ValueError` wrappers stay in their modules.
- Effort: S

### OPT-12: The three taxonomy route sets in `web.py` are one route set written three times
- Severity: Low
- File: src/courseops/web.py:1139-1200 (layers), 1205-1252 (roles), 1254-1313 (leaders)
- What: add / reorder / rename / delete for `poi_category`, `roster_role` and `lead_division` are twelve near-identical handlers: `require_event_admin`, `_json_body`, `_guard(categories.x, ...)`, `conn.commit()` (a no-op, see OPT-15), `conn.close()`, `dict(row)`. They have also drifted: refusing to delete a layer in use is `409` (web.py:1197) while a role or leader in use is `400` (1229, 1293); layer deletion sends a `detail` with "place(s)", the others hand-pluralise.
- Evidence: the bodies differ only in the `categories.*` function name and the error text.
- Why it matters: CLAUDE.md says "a fourth taxonomy goes the same way"; today that is another 60 lines of copy and a fourth chance for the status code to differ. The client (`setup.js`) presumably treats 400 and 409 the same, so pick one.
- Fix: a small table `{"categories": (add, reorder, update, delete, in_use_message), ...}` and one set of four handlers parameterised on `kind`, or an `APIRouter` factory called three times. Unify the in-use status to 409.
- Effort: M

### OPT-13: `web.py` at 2184 lines with `create_app` at 1801 lines mixes five layers
- Severity: Medium
- File: src/courseops/web.py:384-2184
- What: one closure holds: app bootstrap and auth dependencies (384-553, 170 lines), the setup API (554-1551, ~1000 lines), the field API (1552-2060, ~510 lines), the WebSocket (2061-2085), the guides (2091-2110) and the ingest lifecycle (2110-2184). Module level holds `build_state` (159 lines), `_ssid_alerts` and the two ingest handlers, which are snapshot logic, not HTTP. `_tracking_state` and `access_filter_preview` (web.py:998-1043) are ingest logic living inside the closure. Everything is a closure over `app`/`settings`, so nothing can be unit-tested without building the app.
- Evidence: `wc -l`; AST: `create_app` 1801 lines, the longest function in the repo by an order of magnitude; `build_parser` in cli.py is next at 214.
- Why it matters: the CLAUDE.md rule "declare literal routes before parameterised ones" is a symptom of routes being ordered by hand in one file; a route added in the wrong place fails silently. The 38 unused `user` unpackings (OPT-5) are the same helper repeated 38 times because it cannot be a FastAPI dependency in this shape.
- Fix: split into `snapshot.py` (`build_state`, `_ssid_alerts`, `make_position_handler`, `make_nearby_handler`), `setup_api.py` and `field_api.py` as `APIRouter`s taking `settings` through `request.app.state`, `feed.py` (ingest lifecycle, `_tracking_state`, `access_filter_preview`), leaving `web.py` as bootstrap. Turn `require_access` / `require_event_admin` into `Depends` that yield and close the connection, which also removes the 106 hand-written `conn.close()` calls and the leaks in OPT-16.
- Effort: L

### OPT-14: Sessions accumulate forever
- Severity: Low
- File: src/courseops/users.py:415-423 (`start_session` inserts), users.py:454-459 (`purge_expired_sessions`, never called), web.py:685-700 (login)
- What: every login inserts a 30-day session row; nothing deletes expired ones. `resolve_session` (users.py:425-448) checks `expires_at` in Python but leaves the row.
- Evidence: `purge_expired_sessions prod=1 tests=0`.
- Why it matters: slow leak in a table with a `user_id` index; harmless for one club, silly to leave a purge function unused.
- Fix: call `purge_expired_sessions` from `start_session` or from `init_schema` at boot. If not, delete it (OPT-5).
- Effort: S

### OPT-15: `conn.commit()` on an autocommit connection, 18 times, and no transaction anywhere
- Severity: Medium
- File: src/courseops/db.py:17 (`isolation_level=None`); `conn.commit()` at web.py:878, 920, 932, 1109-1135 (`setup_categories`, a pure read), 1149, 1165, 1180, 1195, 1213, 1227, 1247, 1262, 1278, 1291, 1311, 1084; cli.py:423; `grep -n "BEGIN\|SAVEPOINT" src/courseops/*.py` = nothing
- What: the connection is autocommit, so each `conn.commit()` is a no-op that reads as if the preceding statements were one transaction. They are not: `importer.assign_course` (importer.py:254-330, 77 lines, several writes), `db.change_station_key` (db.py:641-708), `admin.delete_event`, `assign_features`, every reorder (OPT-11) and `create_event` + two seeds run as N separate write transactions, each with its own fsync, and each interruptible half way.
- Evidence: `sqlite3.connect(path, isolation_level=None)  # autocommit` and no `BEGIN` in the codebase.
- Why it matters: a crash or a `ValueError` mid-way through `assign_course` leaves a course row with staged features half re-labelled; the reorder docstring's "a half-applied order is worse than none" is exactly what autocommit permits. Also N fsyncs where one would do.
- Fix: a `db.transaction(conn)` context manager (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`) around every multi-statement mutation in admin/importer/categories/db; remove the 18 decorative `commit()` calls. The `setup_categories` GET's `commit()` is simply misleading.
- Effort: M

### OPT-16: Connections that are not closed on the error path
- Severity: Low
- File: src/courseops/web.py:1580-1585 (`manifest`: no try/finally), 1562-1564 (`map_page` fine), 809-838 (`setup_import`: `admin.staged_features` after the try block runs outside it; an exception there leaks `conn`), 1355-1397 (`setup_link_action`: `int(body.get("token_id"))` with a missing id is a `TypeError` -> 500, inside the try so the conn is closed, but the 500 is wrong); `_guard` docstring at web.py:733 says "closing the conn" and does not
- What: the per-request connection pattern relies on 106 hand-written `conn.close()` calls; the ones above miss a path. CPython's refcounting closes them eventually, so it is a smell rather than a leak in practice.
- Evidence: cited lines.
- Why it matters: cleanliness; fixed for free by OPT-13's `Depends` pattern.
- Fix: as OPT-13, or `contextlib.closing` in the meantime.
- Effort: S

### OPT-17: `dropped_off` pickups draw on the map with no colour rule
- Severity: High
- File: src/courseops/static/app.css:913-920 (`.inc--reported/en_route/picked_up/closed/note`), src/courseops/static/app.js:1815-1820 (`inc inc--${incident.status}`), app.js:1864-1869 (only `closed` is removed from the map)
- What: the marker for a pickup at status `dropped_off` gets class `inc--dropped_off`, which has no rule; the base `.inc` is white text, white border, no background. `.inc--closed` exists for the one status that never draws. The list side (`.inc-dot--dropped_off`, app.css:890; `.incident--dropped_off`) is fine, so the two renderings CLAUDE.md says must not drift have drifted.
- Evidence: `grep -n "inc--" app.css` lists five rules and no `dropped_off`; `upsertIncidentMarker` removes only `status === 'closed'`.
- Why it matters: a dropped-off runner shows on the map as a hollow white square with a white bib number, effectively invisible over light tiles, while still listed. Found on the way (section 5 of the brief) but user-visible.
- Fix: add `.inc--dropped_off { background: var(--inc-dropped); }` (the variable already exists for the list dot), or remove dropped-off markers from the map if that was the intent - the CLAUDE.md rule says "in the vehicle still counts as outstanding; delivered does not", which suggests removal like `closed`.
- Effort: S

### OPT-18: Nine hand-rolled POST `fetch`es in `app.js` with three different error conventions; `esc` and `escapeHtml` are the same function
- Severity: Low
- File: src/courseops/static/app.js:1146, 1298, 1506, 1528, 1542, 1881, 1903, 2005, 2042; app.js:668 `escapeHtml` vs setup.js:31 `esc`; setup.js:47-63 `api()`/`post()` exist but only for setup
- What: each field-app POST repeats `method/headers/JSON.stringify/if (!response.ok)`. Only the SSID one (1298-1306) reads `detail` from the body; the others throw `String(response.status)` or `'refused'`, so the 403 messages the server writes ("Staff is read-only.") never reach the screen. `esc`/`escapeHtml` are character-for-character identical.
- Evidence: cited lines.
- Why it matters: the server's per-capability refusal text is wasted; the next endpoint gets a tenth copy.
- Fix: a `post(path, body)` in app.js that reads `detail` and throws it, and a shared `static/util.js` (already the pattern for `icons.js`) for `escapeHtml`.
- Effort: S

### OPT-19: `list_events` and `list_organizations` are N+1; `setup_categories` and `cmd_layers` count per row
- Severity: Low
- File: src/courseops/admin.py:52-68 (4 COUNTs per event), src/courseops/users.py:244-254 (2 per organization), src/courseops/web.py:1098-1136 (3 loops of one COUNT per row), src/courseops/cli.py:429-433 (same count as web.py:1104, duplicated)
- What: per-row `SELECT COUNT(*)` inside a loop over rows.
- Evidence: cited loops.
- Why it matters: setup-only and single digits of rows, so measurable waste only; listed because the brief asks for every N+1 and because `cmd_layers` is a second copy of the web count.
- Fix: one `GROUP BY event_id` / `GROUP BY poi_type` query each; put the layer/role/leader counts into `categories.py` so CLI and web share them.
- Effort: S

### OPT-20: Position messages carry `label` and `category` that no client reads, from a roster snapshot frozen at feed start
- Severity: Low
- File: src/courseops/hub.py:106-108 (`position_message` adds `label`/`category`), src/courseops/web.py:2123-2126 (`roster_by_key` built once in `_ingest_for`), app.js:302-305 / 269-272 (`labelOf`, `categoryOf` read `state.roster` only)
- What: the fields are never read (`grep "\.label\b|\.category\b"` in app.js hits only incident statuses and select options), and if they were they would be stale after any mid-event roster edit because `roster_by_key` is a dict built when the feed started - unlike `ingest.Membership`, which refreshes.
- Evidence: cited lines.
- Why it matters: dead payload per packet per phone; a stale snapshot waiting to be trusted by the next feature.
- Fix: drop the two fields and `roster_by_key`; keep `known_keys` (needed for the one-shot resync), or better, take it from `Membership` so it refreshes too.
- Effort: S (deletion)

### OPT-21: Guides are re-read from disk and re-rendered on every `/help` request
- Severity: Low
- File: src/courseops/guides.py:73-79 (`page_names` globs the directory), 81-84 (`nav_title` reads each `.md` not in `NAV_TITLES`), 87-95 (`load` reads and renders), 318-327 (`page_html` calls `nav_title` per page in the nav)
- What: a `/help/x` request globs the directory, reads up to 11 Markdown files for the nav titles, reads and renders the page. All of it is static content shipped in the package.
- Evidence: no `lru_cache` in guides.py (`grep -n lru_cache src/courseops/guides.py` = nothing).
- Why it matters: cheap (11 small files) and unauthenticated, so it is the one endpoint anyone can hammer; also runs on the event loop (OPT-2).
- Fix: `functools.lru_cache` on `load`, `page_names` and `nav_title` keyed by name (the files do not change while the server runs; development can clear the cache on `_asset_version` change or simply restart).
- Effort: S

### OPT-22: Ingest does three or four write statements per packet on the event loop
- Severity: Low
- File: src/courseops/ingest.py:103-119 (`insert_position`, `bind_heard_ssid`, `log_raw_packet`), db.py:262-304 (`bind_heard_ssid`: two SELECTs and sometimes an UPDATE, for every stored packet including exact-key roster matches)
- What: per stored packet: INSERT position, SELECT taken, SELECT candidates, INSERT raw_packet - each its own autocommit transaction, synchronous, inside the `async for` loop. `bind_heard_ssid` runs for stations that are already on the roster by exact key, where it can never bind.
- Evidence: cited lines; `Membership.roster_keys` already knows whether the key is an exact roster match.
- Why it matters: at APRS volumes (a packet every few seconds at most) this is small; it matters because it shares the loop with OPT-2 and OPT-4 and because OPT-7 halves it for free.
- Fix: skip `bind_heard_ssid` when `report.station_key in membership.roster_keys`; drop `log_raw_packet` (OPT-7); consider `PRAGMA synchronous = NORMAL` on the ingest connection, which WAL makes safe against process crash.
- Effort: S

### OPT-23: Trivia found on the way
- Severity: Low
- File: as listed
- What:
  - web.py:1621 and 1623 set `payload["role"]` twice.
  - web.py:111 `_row_to_dict`, admin.py:26 `_row`, users.py:247 and 274 comprehensions: all are `dict(row)`, which `sqlite3.Row` already supports and which web.py:1104 already uses.
  - ingest.py:125 `_now`, parser.py:45 `_utc_now_iso`, users.py:389 `_now`: three UTC-timestamp helpers with two formats.
  - web.py:820 `tempfile.mkdtemp()` directory is never removed; only the file inside is unlinked. One empty directory per upload in the system temp.
  - web.py:1360 and 1375: `access.revoke(conn, token_id)` / `access.set_label(...)` are keyed by `token_id` alone; `access.revoke` (access.py:235) has no `event_id` in its `WHERE`, so an event admin of one event can revoke or relabel another event's link by id. Flagged here for the security audit; not verified against `may_access_event` beyond reading the two functions.
  - hub.py:59 `unsubscribe` and web.py:2085 are fine, but `Subscription.dropped` is never reset.
- Evidence: cited lines.
- Why it matters: cleanliness, except the revoke scoping which is a correctness issue for a multi-club host.
- Fix: as each line says; add `AND event_id = ?` to `revoke` and `set_label`.
- Effort: S

## Unconfirmed

### OPT-U1: WebSocket liveness without a heartbeat
- Severity: Medium
- File: src/courseops/web.py:2061-2085, src/courseops/static/app.js:2503-2575
- What: the client has no ping and no idle timer; it relies on the socket's `close` event. Uvicorn's `websockets` backend sends protocol pings every 20 s by default, which should surface a dead peer within ~40 s, but that depends on which `--ws` implementation is installed (the `wsproto` backend sends none) and was not verified against the deployed environment.
- Evidence: no `ping`, `pong` or idle timer in app.js; no `ws_ping_interval` in cli.py's `uvicorn.run`.
- Why it matters: the connection badge is the only signal a phone shows stale data.
- Fix: verify which backend the server runs; if not `websockets`, pass `ws_ping_interval=20, ws_ping_timeout=20` to `uvicorn.run`, and add a client-side "no message for 3 minutes -> reconnect" timer.
- Effort: S

## Clean
- JavaScript: all 177 named functions in `app.js`, `setup.js` and `icons.js` are referenced at least once outside their definition; no dead JS function.
- CSS: every class in `app.css` and `guide.css` is produced by the HTML or by a template literal in JS (the `stn--`, `dot--`, `inc--`, `incident--`, `conn--` families are composed dynamically and all resolve, except the missing `inc--dropped_off` rule in OPT-17); only `.swatch-dot` in `setup.css` is orphaned.
- Schema columns: every column in every table is read or written somewhere except `created_at` on six tables (DDL defaults, fine to keep) and `import_batch.imported_at`; no unused table except `admin_token` (OPT-5) and the write-only `raw_packet` (OPT-7).
- Indexes: `latest_position_per_station` and `unexpected_ssids` are covered by `idx_position_station (event_id, station_key, received_at)`; `access_token.token` and `session.token` are unique/PK lookups; `incident`, `lead_sighting`, `roster_status_log`, `import_feature` have indexes matching their WHERE clauses. `poi`, `course`, `roster_role` and `station_exclusion` have no `event_id` index but are tens of rows per event; not worth one.
- `db.connect`: WAL on, `busy_timeout = 5000`, `foreign_keys = ON`, one connection per request; the choices are right, only the per-request WAL pragma cost (OPT-2) and the absence of transactions (OPT-15) are issues.
- Membership refresh in `ingest.py` (5 s throttle, on unknown packet only) is correct and cheap.
- `make_position_handler`'s one-shot `resync` per unknown station is correctly bounded by the `announced` set.
- `build.build_id` is `lru_cache`d; `git describe` runs once.
- Version polling in setup (5 minutes, paused when hidden) and the 15 s age-redraw timer in the field app are proportionate.
- No mutable default arguments, no `TODO`/`FIXME`/`HACK` markers, no bare `except: pass` other than the two justified ones (aprsis.py:145 on `wait_closed`, web.py:2077 on `WebSocketDisconnect`).
- `geo.py`, `kml.py`, `gpx.py`, `parser.py`, `symbols.py`, `labels.py`, `styling.py`, `what3words.py`, `units.py` (bar the two dead helpers), `incidents.py`, `report.py`: no duplication or performance issue found; `report.build` builds one index and locates each incident once.
- The reorder-N-updates loops (OPT-11) are the only per-row write loops; every other `for ... execute` is a seed or a count (OPT-19).
