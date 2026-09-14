# SQL / schema traceability audit (layer 5)

## Summary
Scope: every `execute` / `executemany` / `executescript` in `src/courseops/*.py` (236 statements across access, admin, categories, cli, db, importer, incidents, leaders, progress, report, users, web; discovery, ingest, styling, hub issue no SQL of their own) checked against `src/courseops/schema.sql`, `db._ADDED_COLUMNS` and `db._BACKFILL`. Method: schema loaded into an in-memory SQLite and every static statement compiled with `EXPLAIN` (216/216 compile; the 20 dynamic ones hand-checked), placeholder counts compared with literal parameter counts by AST, and a database created at every historical `schema.sql` commit and every tag (v0.1.0 to v2026.9.4) upgraded through `db.init_schema` and diffed column-by-column against the current schema (all identical, no FK violations). Tests and tools grepped for direct SQL.
Headline: no column, table, placeholder, type, or migration mismatch anywhere. The findings are all about what the correct SQL is asked to do: 1 High (a documented privacy rule violated on the live ingest path), 1 High (setup roster edit duplicates a station), 2 Medium (unscoped by-id statements reachable from another event's admin; rename splits the status log), 4 Low.
Files read in full: schema.sql, db.py, admin.py, users.py, categories.py, incidents.py, importer.py (SQL sections), leaders.py (SQL sections), access.py (SQL sections), parser.py, ingest.py (SQL path), relevant web.py handlers, relevant setup.js handlers.

## Findings

### TR5-1: Editing a roster entry's callsign in setup leaves TWO roster rows for one person
- Severity: High
- File: src/courseops/admin.py:562-571 (calls db.change_station_key then db.upsert_roster_entry); src/courseops/db.py:208-221 (`ON CONFLICT (event_id, station_key)`); src/courseops/db.py:656-667 and 674-691 (bind-not-rename branches)
- What: `save_roster_entry` calls `change_station_key(original, new)`, which for a bare callsign -> its own SSID, or for a different callsign (the "borrowed rig" case), deliberately BINDS (`UPDATE roster SET bound_key = ?`) and leaves `station_key` as it was. It then calls `upsert_roster_entry(conn, event_id, NEW_key, ...)`, whose `ON CONFLICT (event_id, station_key)` finds no row under the new key and INSERTS a second one. The trailing `SELECT ... WHERE station_key = ?` at admin.py:575 returns the duplicate, so the UI shows success.
- Evidence: reproduced with `admin.save_roster_entry(..., {"station_key": "WX0MIK-9", "original_station_key": "WX0MIK", ...})` -> roster holds `('WX0MIK', bound_key='WX0MIK-9')` AND `('WX0MIK-9', bound_key=None)`, same label. Same for `N0CALL-1` -> `K0ABC-5`. Only the same-callsign SSID-to-SSID rename (which really renames) comes out as one row. `setup.js:2231` sends `original_station_key: S.editing` on every edit, so this is the ordinary "fix the callsign" path on the Roster tab. No test covers `save_roster_entry` with `original_station_key` set.
- Why it matters: two roster rows, both tracking the same SSID (`tracking_key` of row 1 == `station_key` of row 2): the NCS stations list shows the operator twice, status set on one does not show on the other, the roster count and the buddy filter carry an extra entry, and there is no error anywhere. Discovered the morning someone corrects a callsign.
- Fix: in `save_roster_entry`, after a `change_station_key` that bound rather than renamed, upsert against the ORIGINAL key (the row that still exists) - i.e. use `row["station_key"]` from the returned row as the key for `upsert_roster_entry` and for the final SELECT. Add a test for each of the three branches.
- Effort: S

### TR5-2: `raw_packet` stores the raw text of NON-rostered stations' packets, against the "not a raw packet, for anyone else" rule
- Severity: High
- File: src/courseops/ingest.py:66-79 (rejection branch runs before the roster check at :91-103); src/courseops/ingest.py:218-222 (`handle_line` called without `log_all_raw`, default True); src/courseops/db.py:365-377
- What: `handle_line` logs every `parse_error` / `no_position` line to `raw_packet` (callsign, full raw text) BEFORE it knows whether the source is on the roster. With the area filter in the live feed this means status, message, telemetry and unparseable packets from every station near the course are persisted verbatim. Position packets from strangers are correctly kept out (`nearby` only), which is what the docstring at :61-65 promises for "not even raw".
- Evidence: reproduced: feeding `K9XYZ-7>APRS,TCPIP*,qAC,X:>Out for a walk` with an unrelated roster -> `raw_packet` row `{'status': 'no_position', 'raw': 'K9XYZ-7>APRS,...>Out for a walk'}`; `position` stays empty. `raw_packet` is write-only: nothing in `src/` ever SELECTs from it (replay was dropped), and it has no rotation.
- Why it matters: CLAUDE.md: "Anything heard that the roster does not know is held in memory ... Not a position, not a raw packet, for anyone else." A club's database - which is backed up nightly and can be handed to an organizer - accumulates the traffic of every ham within the area filter for as long as the feed runs, contradicting the privacy premise the area filter was allowed under. It also grows without bound. (Overlaps with the security audit; reported here because it is a fact about what the INSERT at db.py:373 receives.)
- Fix: move the raw log for rejected lines behind the roster/base-callsign check (log only when `from` callsign is rostered or matches a rostered base), or drop rejected-line logging altogether; given nothing reads the table, consider not writing `stored` lines either (they duplicate `position.raw`). Add a test asserting a stranger's non-position packet leaves `raw_packet` empty.
- Effort: S

### TR5-3: Import-feature and place statements are keyed by id only, so one event's admin can act on another event's rows
- Severity: Medium
- File: src/courseops/importer.py:174 (`SELECT * FROM import_feature WHERE id = ?`), :326 and :369 (`UPDATE import_feature SET status = 'assigned' ... WHERE id = ?`), :377 (`... 'discarded' WHERE id = ?`); src/courseops/admin.py:384-391 (`set_poi_courses` runs before the scoped UPDATE, then `SELECT * FROM poi WHERE id = ?` at :390), :432-441 (`poi_course` insert with the caller's event_id but an unchecked poi_id)
- What: `admin.assign_features` (POST `/api/setup/events/{id}/import/assign`, event-admin gated on `{id}` only) passes feature ids straight to `importer.assign_course/assign_poi/discard`, none of which add `AND event_id = ?`. `update_poi` with only `course_ids` in the payload writes `poi_course` rows and returns the poi row without ever checking the poi belongs to the event.
- Evidence: reproduced: staging a point in event B and calling `admin.assign_features(conn, A, {"kind":"poi","ids":[fid],...})` creates `poi(event_id=A, name='Secret stop', lat/lon from B)` and marks B's feature `assigned` with `poi_id` pointing into A. `admin.update_poi(conn, A, <B's poi id>, {"course_ids": []})` returns B's place name and coordinates.
- Why it matters: every other statement in the codebase carries `event_id` (that is the stated rule), and `may_access_event` is supposed to be the one gate. Once a second club is hosted (#5), an event admin who guesses an id can copy another club's course geometry into their own event, discard the other club's pending review queue, or read a place. Today, with one organization, it is latent.
- Fix: add `AND event_id = ?` to the four `import_feature` statements (pass `event_id` into `get_feature`/`discard`), and make `update_poi` verify the poi is in the event before `set_poi_courses` (or scope the final SELECT). One test per statement.
- Effort: S

### TR5-4: Renaming N0CALL-1 to N0CALL-7 (same callsign, both with SSID) leaves the status history under the old key
- Severity: Medium
- File: src/courseops/db.py:699-702 (`UPDATE roster SET station_key = ?`); src/courseops/db.py:718-723 (`op_status_log` filters `station_key = ?`); src/courseops/schema.sql roster_status_log (keyed by `station_key` text, no FK)
- What: The two bind branches of `change_station_key` keep the log on one key, as CLAUDE.md requires ("the status log must stay on one key"). The third branch - correcting an SSID typo - really renames, and `roster_status_log` is not updated, so the per-station history endpoint (`/api/.../status-log?station_key=N0CALL-7`) returns nothing for the station from then on.
- Evidence: reproduced: `set_op_status(... "N0CALL-1", "active")`, `change_station_key(... "N0CALL-1", "N0CALL-7")` -> `op_status_log(conn, e, "N0CALL-7")` is empty, `op_status_log(conn, e, "N0CALL-1")` has 1 row. The event-wide log (no station filter) still shows it under the old name.
- Why it matters: shift handover reads the station's log; a station corrected mid-event looks like it has never changed status. The rows are not lost, just unreachable through the station view.
- Fix: in the rename branch also `UPDATE roster_status_log SET station_key = ? WHERE event_id = ? AND station_key = ?` (it is a rename, not a rebinding, so moving the history is correct), or record the rename as a log row. Test it.
- Effort: S

### TR5-5: Deleting a place in setup silently deletes every lead-runner sighting at it
- Severity: Medium
- File: src/courseops/schema.sql lead_sighting (`poi_id INTEGER NOT NULL REFERENCES poi(id) ON DELETE CASCADE`); src/courseops/admin.py:527-528 (`DELETE FROM poi` with no check); contrast src/courseops/categories.py:558-584 (`delete_lead_division` REFUSES while sightings exist) and :293-310 (layers refuse while places exist)
- What: `poi.id` cascades into `lead_sighting`, and `delete_poi` is a bare DELETE. Every other delete in the taxonomy family refuses with a count when reports would vanish; this one does not.
- Evidence: reproduced: one sighting at "Aid 3", `admin.delete_poi(conn, e, poi_id)` -> `lead_sighting` count goes 1 -> 0. The leader's position on the NCS panel jumps back to the previous station with nothing on screen to say why.
- Why it matters: CLAUDE.md gives the reason `delete_lead_division` refuses: "those reports would otherwise sit in the database and off the panel with nothing to say where they went." Here they do not even sit in the database. Setup is used mid-event (that is why every setup POST publishes a resync).
- Fix: in `delete_poi`, count `lead_sighting` (and arguably `incident.poi_id` / `roster.poi_id`, which SET NULL and lose the "at Aid 4" fact) and refuse with the count the way `delete_poi_category` does; or at minimum make the UI confirmation state the number.
- Effort: S

### TR5-6: NOT NULL / FK failures reach the setup UI as a 500 instead of a message
- Severity: Low
- File: src/courseops/admin.py:106-109 (`name`, `timezone` -> `.strip() or None` on NOT NULL columns); src/courseops/users.py:300-306 (`user_event` insert with unchecked `event_id`); src/courseops/users.py:187-193 (`organization_id` from the body, FK); src/courseops/web.py:732-737 (`_guard` maps only `ValueError`/`AuthError`)
- What: `update_event` with a whitespace-only name (HTML `required` lets spaces through) or an empty timezone writes NULL to a NOT NULL column; `create_user`/`update_user` with an unknown `event_id` or `organization_id` trips a foreign key. `sqlite3.IntegrityError` is not caught anywhere, and connections are autocommit, so in the create-user case the user row is already committed when the 500 fires.
- Evidence: `values.append((payload.get(name) or "").strip() or None)` at admin.py:109 for `name` and `timezone`, both `NOT NULL` in `event`; `INSERT OR IGNORE INTO user_event` at users.py:303 ignores only UNIQUE conflicts, not FK failures.
- Why it matters: the person setting up sees "Internal Server Error" with no hint; in the user case a half-created account.
- Fix: validate `name`/`timezone` non-empty in `update_event` (raise ValueError like `create_event` does), check `event_ids` against `admin.list_events(conn, organization_id)` before `set_events`, and have `_guard` map `sqlite3.IntegrityError` to a 400.
- Effort: S

### TR5-7: Schema comments name value sets the code has outgrown
- Severity: Low
- File: src/courseops/schema.sql: `access_token.role -- ncs | liaison` (code: ncs, sag, liaison, logistics, staff); `user.role -- system_admin | event_admin` (code adds org_admin); `roster.category -- net_control | aid_station | ... start_finish` (open set from `roster_role`); `raw_packet.status -- stored | no_position | not_rostered | parse_error` (`not_rostered` is never written - by design, see TR5-2)
- What: comments only; no CHECK constraints exist, so nothing breaks, but the next reader of the schema is told the wrong vocabulary.
- Evidence: `access.ROLES` / `users.ROLES` vs the comments quoted above.
- Why it matters: the schema is presented as the place the domain is documented.
- Fix: update the four comments; point `roster.category` at `roster_role.key`.
- Effort: S

### TR5-8: Deleting a course or place leaves its source import feature `assigned` with a NULL target, so it never returns to review
- Severity: Low
- File: src/courseops/schema.sql import_feature (`course_id ... ON DELETE SET NULL`, `poi_id ... ON DELETE SET NULL`, `status` untouched); src/courseops/importer.py:167-170 (`pending_features` filters `status = 'pending'`)
- What: after `DELETE FROM course` / `DELETE FROM poi` the staged feature keeps `status='assigned'` with `course_id`/`poi_id` NULL. It is invisible to the review screen and cannot be reassigned or discarded; the only way back is to import the file again.
- Evidence: FK list from `PRAGMA foreign_key_list` shows SET NULL for both columns; nothing updates `import_feature.status` on delete.
- Why it matters: "delete the course I stitched wrong and redo it" is the obvious workflow the review screen invites, and it needs a re-upload nobody is told about. Not documented in the setup guide.
- Fix: either a trigger / code path that sets `status = 'pending'` when the target is deleted, or a line in the setup guide.
- Effort: S

## Unconfirmed
none

## Clean
- All 216 static SQL statements compile against `schema.sql` (tables and columns exist, aliases resolve); the 20 dynamic ones (`SET` lists, optional `WHERE`, `IN (...)`, `create_event` kwargs, migration DDL) hand-checked - every column name they can produce exists.
- Placeholder count equals parameter count on every statement with a literal tuple/list; dynamic ones build both sides from the same list.
- `_ADDED_COLUMNS`: every entry matches the current schema's type/NOT NULL/default; every column added to `schema.sql` after its first commit has an entry (walked all 17 schema commits); new tables use `CREATE TABLE IF NOT EXISTS`; no `CREATE INDEX` in schema.sql names a column that only a migration adds (matters because `executescript` runs before `ALTER`). `_BACKFILL` references a real column and runs only when that column was just added.
- Upgrade simulation: a database created at every historical schema commit and every tag, upgraded with `init_schema`, matches the current schema column-for-column (name, type, notnull, default) with `PRAGMA foreign_key_check` clean.
- `PRAGMA foreign_keys = ON` is set in `db.connect`, and every connection in `src/` goes through it (only other `sqlite3.connect` is a test reading a backup). `incident_log` cascades from `incident` as CLAUDE.md says; `event` cascades to every event-scoped table; `organization` cascades to events and users (UI confirms with counts).
- UNIQUE vs upsert: `roster` `ON CONFLICT (event_id, station_key)` and `station_exclusion` `ON CONFLICT (event_id, station_key)` match declared UNIQUEs; `INSERT OR IGNORE` on `user_event`, `poi_category`, `roster_role`, `lead_division` match their PK/UNIQUE. `access_token.token`/`session.token` are 32-byte random.
- NOT NULL on INSERT: every INSERT supplies or defaults every NOT NULL column (`poi_category.icon` guarded with `or "pin"`, `lead_sighting.poi_id` pre-checked, `incident.lat/lon` range-checked); the only NULL-into-NOT-NULL path is the UPDATE in TR5-6.
- Enum-like values written match the code's own vocabularies: `incident.status` (STATUSES), `incident.kind` (KINDS, validated), `roster.op_status` (OP_STATUSES, validated), `import_feature.status` literals `pending/assigned/discarded`, `raw_packet.status` (`stored/no_position/parse_error`), `access_token.role` (ROLES), `user.role` (ROLES). No CHECK constraints exist to disagree with.
- Row -> dict: `admin._row`, `web._row_to_dict`, `dict(row)`, `Incident.as_dict` all iterate `row.keys()`; aliased columns (`AS packets`, `AS last_at`, `AS event_id`, `AS m`, `AS c`) are read under the alias by every caller. `resolve_session` selects `u.*` and `_user` reads only user columns.
- Timestamps: one format everywhere - `%Y-%m-%dT%H:%M:%SZ` UTC - whether written by SQL `strftime(...,'now')`, Python (`parser._utc_now_iso`, `ingest._now`, `users.start_session`), or tests/seed; the only parser (`leaders._parse`) and the only string comparison (`session.expires_at`) use the same shape.
- Units: `position.speed_kmh` and `altitude_m` are aprslib's km/h and metres straight through `parser.py`; `distance_m`/`length_m` come from `geo.line_length_m`; nothing converts before storage.
- Indexes: every hot query on `position`, `incident`, `lead_sighting`, `roster_status_log`, `roster` (UNIQUE), `access_token`, `session`, `import_feature` is covered. `poi`, `course`, `poi_category`, `roster_role`, `lead_division` have no `event_id` index beyond their UNIQUEs but hold tens of rows per event; `poi_course` deletes by `(event_id, poi_id)` use neither the PK `(poi_id, course_id)` nor the index `(event_id, course_id)` - same order of magnitude, not worth a finding.
- Tests: direct SQL in `tests/` uses only real tables/columns. `test_styling.py:187-188` builds fake `course`/`poi` tables to exercise the migration step; it proves only that ALTER runs, not that the list is complete - the history walk above is what proves completeness. `test_labels.py:153` drops `show_labels` to test the backfill, correctly.
- `cli.py` `--timezone` defaults to `"UTC"`, `--category` restricted to the default keys, so the CLI cannot write a NULL or an unknown role.

## Appendix: every statement

`OK*` = statement itself is correct; see the named finding for what it is asked to do.

| statement | call | table(s) | columns | params | status | SQL / note |
|---|---|---|---|---|---|---|
| access.py:152 | execute | admin_token | OK | OK | OK | `SELECT token FROM admin_token WHERE revoked = 0 ORDER BY id LIMIT 1` |
| access.py:158 | execute | admin_token | OK | OK | OK | `INSERT INTO admin_token (token, label) VALUES (?, ?)` |
| access.py:167 | execute | admin_token | OK | OK | OK | `SELECT id FROM admin_token WHERE token = ? AND revoked = 0` |
| access.py:172 | execute | admin_token | OK | OK | OK | `UPDATE admin_token SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?` |
| access.py:182 | execute | admin_token | OK | OK | OK | `UPDATE admin_token SET revoked = 1 WHERE revoked = 0` |
| access.py:196 | execute | access_token | OK | OK | OK | `INSERT INTO access_token (event_id, token, role, label) VALUES (?, ?, ?, ?)` |
| access.py:204 | execute | access_token | OK | OK | OK | `SELECT * FROM access_token WHERE event_id = ? ORDER BY role, id` |
| access.py:230 | execute | access_token | OK | OK | OK | `UPDATE access_token SET label = ? WHERE id = ?` |
| access.py:236 | execute | access_token | OK | OK | OK | `UPDATE access_token SET revoked = 1 WHERE id = ?` |
| access.py:253 | execute | access_token, event | OK | OK | OK | `SELECT t.token, t.role, e.id AS event_id, e.slug FROM access_token t JOIN event e ON e....` |
| access.py:265 | execute | access_token | OK | OK | OK | `UPDATE access_token SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE token = ?` |
| admin.py:52 | execute | ? | OK (dynamic, hand-checked) | OK (dynamic) | OK | `{query} ORDER BY id DESC` - optional WHERE organization_id; params match |
| admin.py:55 | execute | course | OK | OK | OK | `SELECT COUNT(*) FROM course WHERE event_id = ?` |
| admin.py:58 | execute | poi | OK | OK | OK | `SELECT COUNT(*) FROM poi WHERE event_id = ?` |
| admin.py:61 | execute | roster | OK | OK | OK | `SELECT COUNT(*) FROM roster WHERE event_id = ?` |
| admin.py:64 | execute | import_feature | OK | OK | OK | `SELECT COUNT(*) FROM import_feature WHERE event_id = ? AND status = 'pending'` |
| admin.py:117 | execute | event | OK (dynamic, hand-checked) | OK (dynamic) | OK* | `UPDATE event SET {', '.join(fields)} WHERE id = ?` - TR5-6: name/timezone may become NULL on NOT NULL |
| admin.py:118 | execute | event | OK | OK | OK | `SELECT * FROM event WHERE id = ?` |
| admin.py:126 | execute | event | OK | OK | OK | `DELETE FROM event WHERE id = ?` |
| admin.py:201 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE id = ? AND event_id = ?` |
| admin.py:207 | execute | course | OK | OK | OK* | `DELETE FROM course WHERE id = ? AND event_id = ?` - TR5-8: import_feature.course_id SET NULL, status stays 'assigned' |
| admin.py:219 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE event_id = ?` |
| admin.py:223 | execute | poi_course | OK | OK | OK | `SELECT poi_id, course_id FROM poi_course WHERE event_id = ?` |
| admin.py:309 | execute | poi | OK | OK | OK | `INSERT INTO poi (event_id, name, poi_type, lat, lon, sort_order) VALUES (?, ?, ?, ?, ?, 0)` |
| admin.py:322 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE id = ?` |
| admin.py:359 | execute | poi | OK | OK | OK | `SELECT name FROM poi WHERE id = ? AND event_id = ?` |
| admin.py:390 | execute | poi | OK | OK | OK* | `SELECT * FROM poi WHERE id = ?` - TR5-3: unscoped read after set_poi_courses |
| admin.py:395 | execute | poi | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE poi SET {', '.join(fields)} WHERE id = ? AND event_id = ?` - dynamic SET: name/poi_type/what3words/label/notes/lat/lon - all real |
| admin.py:400 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE id = ?` |
| admin.py:424 | execute | course | OK | OK | OK | `SELECT id FROM course WHERE event_id = ?` |
| admin.py:432 | execute | poi_course | OK | OK | OK | `DELETE FROM poi_course WHERE event_id = ? AND poi_id = ?` |
| admin.py:437 | execute | poi_course | OK | OK | OK* | `INSERT INTO poi_course (event_id, poi_id, course_id) VALUES (?, ?, ?)` - TR5-3: poi_id not checked against event |
| admin.py:460 | execute | poi | OK | OK | OK | `SELECT id FROM poi WHERE event_id = ?` |
| admin.py:471 | execute | poi | OK | OK | OK | `UPDATE poi SET sort_order = ? WHERE id = ? AND event_id = ?` |
| admin.py:490 | execute | course | OK | OK | OK | `SELECT id FROM course WHERE event_id = ?` |
| admin.py:497 | execute | course | OK | OK | OK | `UPDATE course SET sort_order = ? WHERE id = ? AND event_id = ?` |
| admin.py:520 | execute | poi | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE poi SET poi_type = ? WHERE event_id = ? AND id IN ({placeholders})` - IN list built from len(ids); 2 + len(ids) params |
| admin.py:528 | execute | poi | OK | OK | OK* | `DELETE FROM poi WHERE id = ? AND event_id = ?` - TR5-5: cascades lead_sighting |
| admin.py:534 | execute | poi | OK | OK | OK | `SELECT id, name FROM poi WHERE event_id = ?` |
| admin.py:575 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| admin.py:583 | execute | roster | OK | OK | OK | `DELETE FROM roster WHERE event_id = ? AND station_key = ?` |
| categories.py:112 | execute | poi_category | OK | OK | OK | `SELECT 1 FROM poi_category WHERE event_id = ? LIMIT 1` |
| categories.py:119 | execute | poi_category | OK | OK | OK | `INSERT INTO poi_category (event_id, key, name, staffed, icon, color, sort_order, visibl...` |
| categories.py:132 | execute | poi | OK | OK | OK | `SELECT DISTINCT poi_type FROM poi WHERE event_id = ?` |
| categories.py:138 | execute | poi_category | OK | OK | OK | `INSERT OR IGNORE INTO poi_category (event_id, key, name, staffed, icon, color, sort_ord...` |
| categories.py:151 | execute | poi_category | OK | OK | OK | `SELECT * FROM poi_category WHERE event_id = ? ORDER BY sort_order, name` |
| categories.py:165 | execute | poi_category | OK | OK | OK | `SELECT key FROM poi_category WHERE event_id = ? AND staffed = 1` |
| categories.py:186 | execute | poi_category | OK | OK | OK | `SELECT 1 FROM poi_category WHERE event_id = ? AND key = ?` |
| categories.py:193 | execute | poi_category | OK | OK | OK | `SELECT COALESCE(MAX(sort_order), 0) AS m FROM poi_category WHERE event_id = ?` |
| categories.py:197 | execute | poi_category | OK | OK | OK | `INSERT INTO poi_category (event_id, key, name, staffed, icon, color, sort_order, visibl...` |
| categories.py:213 | execute | poi_category | OK | OK | OK | `SELECT * FROM poi_category WHERE event_id = ? AND key = ?` |
| categories.py:256 | execute | poi_category | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE poi_category SET {', '.join(fields)} WHERE event_id = ? AND key = ?` - dynamic SET: name/staffed/visible/show_labels/icon/color/sort_order - all real |
| categories.py:278 | execute | poi_category | OK | OK | OK | `SELECT key FROM poi_category WHERE event_id = ?` |
| categories.py:285 | execute | poi_category | OK | OK | OK | `UPDATE poi_category SET sort_order = ? WHERE event_id = ? AND key = ?` |
| categories.py:301 | execute | poi | OK | OK | OK | `SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = ?` |
| categories.py:307 | execute | poi_category | OK | OK | OK | `DELETE FROM poi_category WHERE event_id = ? AND key = ?` |
| categories.py:323 | execute | roster_role | OK | OK | OK | `SELECT 1 FROM roster_role WHERE event_id = ? LIMIT 1` |
| categories.py:329 | execute | roster_role | OK | OK | OK | `INSERT OR IGNORE INTO roster_role (event_id, key, name, sort_order) VALUES (?, ?, ?, ?)` |
| categories.py:346 | execute | roster_role | OK | OK | OK | `SELECT * FROM roster_role WHERE event_id = ? ORDER BY sort_order, name` |
| categories.py:374 | execute | roster_role | OK | OK | OK | `SELECT 1 FROM roster_role WHERE event_id = ? AND key = ?` |
| categories.py:380 | execute | roster_role | OK | OK | OK | `SELECT COALESCE(MAX(sort_order), 0) AS m FROM roster_role WHERE event_id = ?` |
| categories.py:384 | execute | roster_role | OK | OK | OK | `INSERT INTO roster_role (event_id, key, name, sort_order) VALUES (?, ?, ?, ?)` |
| categories.py:389 | execute | roster_role | OK | OK | OK | `SELECT * FROM roster_role WHERE event_id = ? AND key = ?` |
| categories.py:403 | execute | roster | OK | OK | OK | `SELECT COUNT(*) AS c FROM roster WHERE event_id = ? AND category = ?` |
| categories.py:409 | execute | roster_role | OK | OK | OK | `DELETE FROM roster_role WHERE event_id = ? AND key = ?` |
| categories.py:421 | execute | roster_role | OK | OK | OK | `SELECT 1 FROM roster_role WHERE event_id = ? AND key = ?` |
| categories.py:427 | execute | roster_role | OK | OK | OK | `UPDATE roster_role SET name = ? WHERE event_id = ? AND key = ?` |
| categories.py:431 | execute | roster_role | OK | OK | OK | `SELECT * FROM roster_role WHERE event_id = ? AND key = ?` |
| categories.py:449 | execute | lead_division | OK | OK | OK | `SELECT 1 FROM lead_division WHERE event_id = ? LIMIT 1` |
| categories.py:454 | execute | lead_division | OK | OK | OK | `INSERT OR IGNORE INTO lead_division (event_id, key, name, sort_order) VALUES (?, ?, ?, ?)` |
| categories.py:466 | execute | lead_sighting | OK | OK | OK | `SELECT DISTINCT division FROM lead_sighting WHERE event_id = ?` |
| categories.py:473 | execute | lead_division | OK | OK | OK | `INSERT OR IGNORE INTO lead_division (event_id, key, name, sort_order) VALUES (?, ?, ?, ...` |
| categories.py:483 | execute | lead_division | OK | OK | OK | `SELECT * FROM lead_division WHERE event_id = ? ORDER BY sort_order, name` |
| categories.py:513 | execute | lead_division | OK | OK | OK | `SELECT 1 FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:519 | execute | lead_division | OK | OK | OK | `SELECT COALESCE(MAX(sort_order), 0) AS m FROM lead_division WHERE event_id = ?` |
| categories.py:523 | execute | lead_division | OK | OK | OK | `INSERT INTO lead_division (event_id, key, name, sort_order) VALUES (?, ?, ?, ?)` |
| categories.py:528 | execute | lead_division | OK | OK | OK | `SELECT * FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:542 | execute | lead_division | OK | OK | OK | `SELECT 1 FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:548 | execute | lead_division | OK | OK | OK | `UPDATE lead_division SET name = ? WHERE event_id = ? AND key = ?` |
| categories.py:552 | execute | lead_division | OK | OK | OK | `SELECT * FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:567 | execute | lead_division | OK | OK | OK | `SELECT 1 FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:573 | execute | lead_sighting | OK | OK | OK | `SELECT COUNT(*) AS c FROM lead_sighting WHERE event_id = ? AND division = ?` |
| categories.py:580 | execute | lead_division | OK | OK | OK | `DELETE FROM lead_division WHERE event_id = ? AND key = ?` |
| categories.py:601 | execute | lead_division | OK | OK | OK | `SELECT key FROM lead_division WHERE event_id = ?` |
| categories.py:608 | execute | lead_division | OK | OK | OK | `UPDATE lead_division SET sort_order = ? WHERE event_id = ? AND key = ?` |
| cli.py:253 | execute | roster | OK | OK | OK | `DELETE FROM roster WHERE event_id = ? AND station_key = ?` |
| cli.py:430 | execute | poi | OK | OK | OK | `SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = ?` |
| cli.py:454 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| cli.py:461 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE event_id = ?` |
| cli.py:509 | execute | poi | OK | OK | OK | `SELECT name FROM poi WHERE id = ?` |
| cli.py:563 | execute | poi | OK | OK | OK | `UPDATE poi SET what3words = ? WHERE id = ? AND event_id = ?` |
| db.py:19 | execute | - | n/a | n/a | OK | `PRAGMA journal_mode = WAL` |
| db.py:20 | execute | - | n/a | n/a | OK | `PRAGMA foreign_keys = ON` |
| db.py:21 | execute | - | n/a | n/a | OK | `PRAGMA busy_timeout = 5000` |
| db.py:63 | execute | - | n/a | n/a | OK | `PRAGMA table_info({table})` - PRAGMA table_info, internal constant |
| db.py:72 | execute | sqlite_master | OK | OK | OK | `SELECT name FROM sqlite_master WHERE type = 'table'` |
| db.py:80 | execute | ? | OK (dynamic, hand-checked) | OK | OK | `ALTER TABLE {table} ADD COLUMN {column} {ddl}` - ALTER TABLE from _ADDED_COLUMNS; every entry matches schema type/default (verified by upgrade simulation) |
| db.py:83 | execute | ? | OK (dynamic, hand-checked) | OK | OK | `{backfill}` - _BACKFILL: poi_category.show_labels from staffed; column real |
| db.py:89 | executescript | - | n/a | n/a | OK | `<dynamic>` - executescript(schema.sql); runs BEFORE _ADDED_COLUMNS - no index names a migrated column (verified) |
| db.py:113 | execute | poi | OK | OK | OK | `SELECT id, notes FROM poi WHERE notes LIKE '%<%>%'` |
| db.py:117 | execute | poi | OK | OK | OK | `UPDATE poi SET notes = ? WHERE id = ?` |
| db.py:133 | execute | event | OK | OK | OK | `SELECT id FROM event` |
| db.py:145 | execute | event | OK | OK | OK | `SELECT COUNT(*) FROM event WHERE organization_id IS NULL` |
| db.py:150 | execute | organization | OK | OK | OK | `SELECT id FROM organization ORDER BY id LIMIT 1` |
| db.py:154 | execute | organization | OK | OK | OK | `INSERT INTO organization (slug, name) VALUES ('default', 'Default')` |
| db.py:160 | execute | event | OK | OK | OK | `UPDATE event SET organization_id = ? WHERE organization_id IS NULL` |
| db.py:171 | execute | event | OK (dynamic, hand-checked) | OK (dynamic) | OK | `INSERT INTO event ({', '.join(columns)}) VALUES ({placeholders})` - columns from kwargs; callers pass organization_id/event_date/timezone/center_lat/center_lon/aprs_filter_extra - all real |
| db.py:188 | execute | event | OK | OK | OK | `SELECT * FROM event WHERE slug = ?` |
| db.py:192 | execute | event | OK | OK | OK | `SELECT * FROM event WHERE is_active = 1 ORDER BY id` |
| db.py:208 | execute | roster | OK | OK | OK* | `INSERT INTO roster (event_id, station_key, display_label, category, expects_aprs, opera...` - TR5-1: called after change_station_key with the NEW key -> duplicate row |
| db.py:254 | execute | roster | OK | OK | OK | `SELECT station_key FROM roster WHERE event_id = ? AND (station_key = ? OR bound_key = ?)` |
| db.py:279 | execute | roster | OK | OK | OK | `SELECT 1 FROM roster WHERE event_id = ? AND (station_key = ? OR bound_key = ?)` |
| db.py:288 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ? AND bound_key IS NULL` |
| db.py:297 | execute | roster | OK | OK | OK | `UPDATE roster SET bound_key = ? WHERE event_id = ? AND station_key = ?` |
| db.py:301 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:313 | execute | roster | OK | OK | OK | `UPDATE roster SET bound_key = NULL WHERE event_id = ? AND station_key = ?` |
| db.py:321 | execute | roster | OK | OK | OK | `SELECT bound_key FROM roster WHERE event_id = ? AND bound_key IS NOT NULL` |
| db.py:329 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label` |
| db.py:337 | execute | roster | OK | OK | OK | `SELECT station_key FROM roster WHERE event_id = ? AND expects_aprs = 1 ORDER BY station...` |
| db.py:348 | execute | position | OK | OK | OK | `INSERT INTO position ( event_id, station_key, received_at, lat, lon, course_deg, speed_...` |
| db.py:373 | execute | raw_packet | OK | OK | OK* | `INSERT INTO raw_packet (event_id, received_at, raw, status, error) VALUES (?, ?, ?, ?, ?)` - TR5-2: written for non-rostered stations too |
| db.py:383 | execute | position | OK | OK | OK | `SELECT * FROM position WHERE event_id = ? ORDER BY received_at DESC, id DESC LIMIT ?` |
| db.py:393 | execute | position | OK | OK | OK | `SELECT p.* FROM position p JOIN ( SELECT station_key, MAX(id) AS max_id FROM position W...` |
| db.py:434 | execute | event | OK | OK | OK | `UPDATE event SET ingest_enabled = ? WHERE slug = ?` |
| db.py:443 | execute | event | OK | OK | OK | `SELECT slug FROM event WHERE ingest_enabled = 1 ORDER BY id` |
| db.py:473 | execute | roster | OK | OK | OK | `SELECT op_status FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:478 | execute | roster | OK | OK | OK | `UPDATE roster SET op_status = ?, op_status_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), o...` |
| db.py:493 | execute | roster_status_log | OK | OK | OK | `INSERT INTO roster_status_log (event_id, station_key, by, from_status, to_status) VALUE...` |
| db.py:500 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:516 | execute | poi | OK | OK | OK | `SELECT 1 FROM poi WHERE id = ? AND event_id = ?` |
| db.py:522 | execute | roster | OK | OK | OK | `UPDATE roster SET poi_id = ? WHERE event_id = ? AND station_key = ?` |
| db.py:528 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:538 | execute | station_exclusion | OK | OK | OK | `SELECT station_key FROM station_exclusion WHERE event_id = ?` |
| db.py:548 | execute | station_exclusion | OK | OK | OK | `INSERT INTO station_exclusion (event_id, station_key, reason) VALUES (?, ?, ?) ON CONFL...` |
| db.py:559 | execute | station_exclusion | OK | OK | OK | `DELETE FROM station_exclusion WHERE event_id = ? AND station_key = ?` |
| db.py:567 | execute | station_exclusion | OK | OK | OK | `SELECT * FROM station_exclusion WHERE event_id = ? ORDER BY station_key` |
| db.py:606 | execute | position | OK | OK | OK | `SELECT p.station_key, COUNT(*) AS packets, MAX(p.received_at) AS last_at, MAX(p.symbol_...` |
| db.py:657 | execute | roster | OK | OK | OK | `UPDATE roster SET bound_key = ? WHERE event_id = ? AND station_key = ?` |
| db.py:661 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:675 | execute | roster | OK | OK | OK | `SELECT station_key FROM roster WHERE event_id = ? AND (station_key = ? OR bound_key = ?)` |
| db.py:682 | execute | roster | OK | OK | OK | `UPDATE roster SET bound_key = ? WHERE event_id = ? AND station_key = ?` |
| db.py:688 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:692 | execute | roster | OK | OK | OK | `SELECT 1 FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:699 | execute | roster | OK | OK | OK* | `UPDATE roster SET station_key = ? WHERE event_id = ? AND station_key = ?` - TR5-4: rename path; roster_status_log rows stay under old key |
| db.py:705 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
| db.py:723 | execute | ? | OK (dynamic, hand-checked) | OK (dynamic) | OK | `{query} ORDER BY at, id` - optional AND station_key; params match |
| db.py:734 | execute | roster | OK | OK | OK | `SELECT station_key FROM roster WHERE event_id = ? ORDER BY station_key` |
| importer.py:100 | execute | poi_category | OK | OK | OK | `UPDATE poi_category SET key = ?, visible = ? WHERE id = ?` |
| importer.py:123 | execute | import_batch | OK | OK | OK | `INSERT INTO import_batch (event_id, filename, source_kind) VALUES (?, ?, ?)` |
| importer.py:143 | execute | import_feature | OK | OK | OK | `INSERT INTO import_feature ( batch_id, event_id, name, folder, geom_type, geojson, leng...` |
| importer.py:170 | execute | ? | OK (dynamic, hand-checked) | OK (dynamic) | OK | `{query} ORDER BY id` - base has 1 ?, optional literal filter |
| importer.py:174 | execute | import_feature | OK | OK | OK* | `SELECT * FROM import_feature WHERE id = ?` - TR5-3: by id only, no event_id |
| importer.py:188 | execute | course | OK | OK | OK | `SELECT color FROM course WHERE event_id = ?` |
| importer.py:196 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| importer.py:245 | execute | course | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE course SET {', '.join(updates)} WHERE id = ? AND event_id = ?` - dynamic SET: color/dash_pattern/name/sort_order - all real |
| importer.py:251 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE id = ?` |
| importer.py:310 | execute | course | OK | OK | OK | `SELECT COALESCE(MAX(sort_order), -1) + 1 FROM course WHERE event_id = ?` |
| importer.py:315 | execute | course | OK | OK | OK | `INSERT INTO course (event_id, name, color, dash_pattern, geojson, distance_m, sort_orde...` |
| importer.py:326 | executemany | import_feature | OK | OK (dynamic) | OK* | `UPDATE import_feature SET status = 'assigned', course_id = ? WHERE id = ?` - TR5-3: by id only, no event_id |
| importer.py:358 | execute | poi | OK | OK | OK | `INSERT INTO poi (event_id, name, poi_type, lat, lon, what3words, notes) VALUES (?, ?, ?...` |
| importer.py:369 | execute | import_feature | OK | OK | OK* | `UPDATE import_feature SET status = 'assigned', poi_id = ? WHERE id = ?` - TR5-3: by id only, no event_id |
| importer.py:377 | executemany | import_feature | OK | OK (dynamic) | OK* | `UPDATE import_feature SET status = 'discarded' WHERE id = ?` - TR5-3: by id only, no event_id |
| importer.py:388 | execute | import_feature | OK | OK | OK | `SELECT geojson FROM import_feature WHERE event_id = ? AND status != 'discarded'` |
| incidents.py:129 | execute | poi | OK | OK | OK | `SELECT 1 FROM poi WHERE id = ? AND event_id = ?` |
| incidents.py:136 | execute | incident | OK | OK | OK | `INSERT INTO incident (event_id, kind, bib, lat, lon, poi_id, note, reported_by, status_...` |
| incidents.py:168 | execute | incident | OK | OK | OK | `UPDATE incident SET status = ?, status_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), statu...` |
| incidents.py:218 | execute | incident | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE incident SET {', '.join(updates)} WHERE id = ? AND event_id = ?` - dynamic SET: bib/note/assigned_to/lat/lon - all real |
| incidents.py:227 | execute | incident | OK | OK | OK | `SELECT * FROM incident WHERE id = ? AND event_id = ?` |
| incidents.py:256 | execute | incident | OK | OK | OK | `DELETE FROM incident WHERE id = ? AND event_id = ?` |
| incidents.py:275 | execute | ? | OK (dynamic, hand-checked) | OK (dynamic) | OK | `{query}` - base has 1 ?, optional literal filter |
| incidents.py:289 | execute | incident | OK | OK | OK | `SELECT COUNT(*) AS c FROM incident WHERE event_id = ? AND kind = ? AND status NOT IN ('...` |
| incidents.py:300 | execute | incident_log | OK | OK | OK | `SELECT * FROM incident_log WHERE incident_id = ? ORDER BY at, id` |
| incidents.py:313 | execute | incident_log | OK | OK | OK | `INSERT INTO incident_log (incident_id, by, action, detail) VALUES (?, ?, ?, ?)` |
| leaders.py:140 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE id = ? AND event_id = ?` |
| leaders.py:152 | execute | course | OK | OK | OK | `UPDATE course SET bib_color = ?, bib_color_name = ? WHERE id = ?` |
| leaders.py:156 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE id = ?` |
| leaders.py:173 | execute | course | OK | OK | OK | `SELECT 1 FROM course WHERE id = ? AND event_id = ?` |
| leaders.py:178 | execute | poi | OK | OK | OK | `SELECT 1 FROM poi WHERE id = ? AND event_id = ?` |
| leaders.py:184 | execute | lead_sighting | OK | OK | OK | `INSERT INTO lead_sighting (event_id, course_id, division, poi_id, bib, by) VALUES (?, ?...` |
| leaders.py:192 | execute | lead_sighting | OK | OK | OK | `SELECT * FROM lead_sighting WHERE id = ?` |
| leaders.py:201 | execute | lead_sighting | OK | OK | OK | `SELECT id FROM lead_sighting WHERE event_id = ? AND course_id = ? AND division = ? ORDE...` |
| leaders.py:208 | execute | lead_sighting | OK | OK | OK | `DELETE FROM lead_sighting WHERE id = ?` |
| leaders.py:226 | execute | lead_sighting | OK | OK | OK | `DELETE FROM lead_sighting WHERE event_id = ? AND course_id = ? AND division = ?` |
| leaders.py:237 | execute | lead_sighting | OK | OK | OK | `SELECT * FROM lead_sighting WHERE event_id = ? AND course_id = ? AND division = ? ORDER...` |
| leaders.py:252 | execute | poi, poi_category | OK | OK | OK | `SELECT p.* FROM poi p JOIN poi_category c ON c.event_id = p.event_id AND c.key = p.poi_...` |
| leaders.py:284 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| leaders.py:301 | execute | poi_course | OK | OK | OK | `SELECT poi_id, course_id FROM poi_course WHERE event_id = ?` |
| progress.py:104 | execute | course | OK | OK | OK | `SELECT id, name, geojson FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| report.py:83 | execute | event | OK | OK | OK | `SELECT * FROM event WHERE id = ?` |
| report.py:91 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE event_id = ?` |
| report.py:139 | execute | course | OK | OK | OK | `SELECT name, color, geojson FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| users.py:179 | execute | user | OK | OK | OK | `SELECT 1 FROM user WHERE username = ?` |
| users.py:187 | execute | user | OK | OK | OK* | `INSERT INTO user (username, password_hash, role, display_name, organization_id) VALUES ...` - TR5-6: unknown organization_id -> FK failure -> 500 |
| users.py:203 | execute | user | OK | OK | OK | `SELECT * FROM user WHERE id = ?` |
| users.py:211 | execute | user | OK | OK | OK | `SELECT * FROM user ORDER BY role, username` |
| users.py:224 | execute | user | OK | OK | OK | `UPDATE user SET password_hash = ? WHERE id = ?` |
| users.py:228 | execute | session | OK | OK | OK | `DELETE FROM session WHERE user_id = ?` |
| users.py:232 | execute | user | OK | OK | OK | `UPDATE user SET is_active = ? WHERE id = ?` |
| users.py:235 | execute | session | OK | OK | OK | `DELETE FROM session WHERE user_id = ?` |
| users.py:239 | execute | user | OK | OK | OK | `DELETE FROM user WHERE id = ?` |
| users.py:244 | execute | organization | OK | OK | OK | `SELECT * FROM organization ORDER BY name` |
| users.py:248 | execute | event | OK | OK | OK | `SELECT COUNT(*) FROM event WHERE organization_id = ?` |
| users.py:251 | execute | user | OK | OK | OK | `SELECT COUNT(*) FROM user WHERE organization_id = ?` |
| users.py:266 | execute | organization | OK | OK | OK | `SELECT 1 FROM organization WHERE slug = ?` |
| users.py:268 | execute | organization | OK | OK | OK | `INSERT INTO organization (slug, name, contact) VALUES (?, ?, ?)` |
| users.py:272 | execute | organization | OK | OK | OK | `SELECT * FROM organization WHERE id = ?` |
| users.py:283 | execute | ? | OK (dynamic, hand-checked) | OK (dynamic) | OK | `{query}` - optional AND id != ?; params match |
| users.py:287 | execute | user | OK | OK | OK | `SELECT 1 FROM user LIMIT 1` |
| users.py:294 | execute | user_event | OK | OK | OK | `SELECT event_id FROM user_event WHERE user_id = ?` |
| users.py:301 | execute | user_event | OK | OK | OK | `DELETE FROM user_event WHERE user_id = ?` |
| users.py:303 | execute | user_event | OK | OK | OK* | `INSERT OR IGNORE INTO user_event (user_id, event_id) VALUES (?, ?)` - TR5-6: FK failure -> 500; no org check on event_ids |
| users.py:330 | execute | organization | OK (dynamic, hand-checked) | OK (dynamic) | OK | `UPDATE organization SET {', '.join(fields)} WHERE id = ?` - dynamic SET: name/contact - all real |
| users.py:335 | execute | organization | OK | OK | OK | `SELECT * FROM organization WHERE id = ?` |
| users.py:342 | execute | event | OK | OK | OK | `SELECT organization_id FROM event WHERE id = ?` |
| users.py:394 | execute | user | OK | OK | OK | `SELECT * FROM user WHERE username = ?` |
| users.py:408 | execute | user | OK | OK | OK | `UPDATE user SET last_login = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?` |
| users.py:418 | execute | session | OK | OK | OK | `INSERT INTO session (token, user_id, expires_at) VALUES (?, ?, ?)` |
| users.py:428 | execute | session, user | OK | OK | OK | `SELECT s.token, s.expires_at, u.* FROM session s JOIN user u ON u.id = s.user_id WHERE ...` |
| users.py:440 | execute | session | OK | OK | OK | `DELETE FROM session WHERE token = ?` |
| users.py:443 | execute | session | OK | OK | OK | `UPDATE session SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE token = ?` |
| users.py:451 | execute | session | OK | OK | OK | `DELETE FROM session WHERE token = ?` |
| users.py:455 | execute | session | OK | OK | OK | `DELETE FROM session WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')` |
| web.py:230 | execute | event | OK | OK | OK | `SELECT * FROM event WHERE id = ?` |
| web.py:244 | execute | course | OK | OK | OK | `SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id` |
| web.py:254 | execute | poi | OK | OK | OK | `SELECT * FROM poi WHERE event_id = ?` |
| web.py:267 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label` |
| web.py:581 | execute | event | OK | OK | OK | `SELECT 1 FROM event LIMIT 1` |
| web.py:1006 | execute | event | OK | OK | OK | `SELECT slug, ingest_enabled FROM event WHERE id = ?` |
| web.py:1068 | execute | event | OK | OK | OK | `SELECT slug FROM event WHERE id = ?` |
| web.py:1104 | execute | poi | OK | OK | OK | `SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = ?` |
| web.py:1115 | execute | roster | OK | OK | OK | `SELECT COUNT(*) AS c FROM roster WHERE event_id = ? AND category = ?` |
| web.py:1126 | execute | lead_sighting | OK | OK | OK | `SELECT COUNT(*) AS c FROM lead_sighting WHERE event_id = ? AND division = ?` |
| web.py:1343 | execute | event | OK | OK | OK | `SELECT slug FROM event WHERE id = ?` |
| web.py:1446 | execute | organization | OK | OK | OK | `DELETE FROM organization WHERE id = ?` - cascades events, users; UI confirms |
| web.py:1581 | execute | event | OK | OK | OK | `SELECT name FROM event WHERE id = ?` |
| web.py:1872 | execute | roster | OK | OK | OK | `SELECT * FROM roster WHERE event_id = ? AND station_key = ?` |
