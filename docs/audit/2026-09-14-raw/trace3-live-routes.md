# Live routes traceability audit (layer 3)

## Summary
Scope: every handler in `src/courseops/web.py` from `GET /` (1552) through the WebSocket (2061) and `/help` (2091-2105), plus `lifespan` (391-410), the `publish_setup_changes` middleware (445-456), `make_position_handler` (120-150), `make_nearby_handler` (159-190), `_ssid_alerts` (193-220), `build_state` (223-381) and the ingest lifecycle (`_ingest_for` .. `_stop_ingest`, 2118-2181). Every call into `db.py`, `access.py`, `hub.py`, `incidents.py`, `leaders.py`, `progress.py`, `categories.py`, `labels.py`, `symbols.py`, `guides.py`, `ingest.py` and `parser.py` was traced to the callee's signature and return shape; `units.py`, `discovery.py`, `what3words.py` and `styling.py` are not called from this range (styling only via `leaders.set_bib_color`).
Files read: web.py, access.py, hub.py, db.py, incidents.py, leaders.py, progress.py, categories.py, symbols.py, labels.py, styling.py, guides.py, ingest.py, parser.py, config.py, cli.py (cmd_serve), schema.sql, app.js (message dispatch only).
Counts: Critical 1, High 0, Medium 3, Low 6. One finding (TR3-1) was reproduced with a script against the venv (no network).

## Findings

### TR3-1: A `SystemExit` from `run_ingest` escapes the ingest task and kills the whole server, and the persisted switch makes it a boot loop
- Severity: Critical
- File: src/courseops/web.py:2142-2155 (`_supervise_ingest`), src/courseops/ingest.py:179 and 194-198 (`run_ingest`), src/courseops/config.py:70-74 (`require_callsign`), src/courseops/web.py:391-406 (`lifespan`), src/courseops/web.py:1083-1091 (`setup_set_tracking`)
- What: `_supervise_ingest` catches `asyncio.CancelledError` and `Exception`. `run_ingest` signals "no callsign" and "no roster, no course, no extra filter" with `SystemExit`, which is a `BaseException`. asyncio re-raises `SystemExit`/`KeyboardInterrupt` out of `Task.__step` and out of `run_forever`, so the exception is not recorded in `app.state.ingest_errors`; it propagates through uvicorn's `asyncio.run` and terminates the process. `setup_set_tracking` guards only the callsign case; the empty-event case is reachable from the UI, and because `db.set_ingest_enabled(conn, slug, True)` runs BEFORE `start_ingest` (web.py:1089-1090), `lifespan` starts the same feed on the next boot and dies again.
- Evidence: web.py:2148 `except Exception as exc:` with the comment "A missing callsign lands here" - it does not. ingest.py:195 `raise SystemExit(f"Event {event_slug!r} has no APRS-expecting roster entries, no course and no extra filter. ...")`. Reproduced: `create_app` + `start_ingest("empty")` on an event with no roster/course -> `asyncio.run` exits with `SystemExit: Event 'empty' has no APRS-expecting roster entries ...` (script `scratchpad/tr3_sysexit.py`).
- Why it matters: An officer who creates an event and flips Tracking on before importing the course (or a club whose `.env` lost its callsign between deploys while an event has `ingest_enabled = 1`) takes the whole site down, every role page with it, and systemd restarts it into the same crash until someone edits the database by hand. The setup page cannot show the reason because the process that would show it is gone.
- Fix: In `_supervise_ingest` catch `BaseException` other than `CancelledError` (or, better, have `run_ingest` raise a module-level `IngestError(RuntimeError)` and keep `SystemExit` only in `cli.py`). In `setup_set_tracking`, refuse with a 400 when `state["tracked"] == 0 and state["area_mi"] is None and not aprs_filter_extra`, the same way the callsign is refused. Add a test that starts the feed on an empty event and asserts the loop survives and `ingest_errors[slug]` is set.
- Effort: S

### TR3-2: After NCS presses Ignore, the ignored station's packets keep being stored and pushed as `position` until an UNKNOWN station happens to beacon
- Severity: Medium
- File: src/courseops/ingest.py:82-87 and 217-228, src/courseops/web.py:1886-1900 (`ignore_ssid`), src/courseops/static/app.js:2556-2559
- What: `handle_line` drops excluded keys using `membership.excluded`, but `Membership.refresh()` is called only inside `if nearby:` (ingest.py:223-226), i.e. only when a packet from a station the roster does NOT know arrives. An ignored SSID under a rostered callsign is still `known` via `base_callsigns` (ingest.py:93-94), so it is stored (`db.insert_position`) and fanned out via `on_position` -> `hub.publish(position)`; the client re-adds the marker unconditionally (app.js:2557). `/state` filters it (web.py:280-296), so the marker disappears on the resync Ignore triggers and reappears on the digipeater's next beacon. The comment at ingest.py:83-84 ("the membership refresh picks that up within seconds without a reconnect") is not what the code does.
- Evidence: ingest.py:226 `membership.refresh()` is the only non-forced call; it is reached only when `nearby` is non-empty.
- Why it matters: The domain rule says `station_exclusion` must hide stored positions, and this is the "ignore leaves the thing on the map" failure it names. With the area filter delivering the public every few seconds the window is short, but on a quiet band (early morning, small town) an ignored igate keeps beaconing every 10-30 minutes and reappearing after each Ignore, which teaches NCS the button does not work.
- Fix: Call `membership.refresh()` (rate-limited as it already is) at the top of every iteration, or at least before the excluded check; alternatively have `ignore_ssid`/`adopt_ssid` poke a shared "membership dirty" flag the loop honours. Add a test: exclude a rostered-base SSID mid-stream with no unknown traffic and assert the next packet is neither stored nor published.
- Effort: S

### TR3-3: Enabling a second event's feed leaves the first event's persisted switch at "on"; on restart the lower-id event is started and then killed
- Severity: Medium
- File: src/courseops/web.py:2157-2171 (`_start_ingest`), 391-406 (`lifespan`), 1054-1094 (`setup_set_tracking`), db.py:428-447
- What: `_start_ingest(slug)` stops every other running task but never calls `db.set_ingest_enabled(other, False)`. So the displaced event keeps `ingest_enabled = 1`; `_tracking_state` for it reports `enabled: True, running: False, error: ""` (web.py:1020-1029). `lifespan` then iterates `events_wanting_ingest()` in id order and starts each, so with two events flagged the first is started, immediately cancelled by the second, and the server settles on whichever has the higher id - not the one someone last switched on.
- Evidence: web.py:2165-2167 `for running in list(app.state.ingest_tasks): if running != slug: await _stop_ingest(running)` - no database write; db.py:440-447 returns every flagged slug.
- Why it matters: Two events on one server is the multi-club case that #5 is about, but a club running a rehearsal event and the real one already hits it: the rehearsal's page reads "Tracking on - but not connected" with an empty error, and after a deploy the real event's feed may be the one that got cancelled.
- Fix: In `_start_ingest`, persist `set_ingest_enabled(other, False)` for each feed it stops (it has `settings.db_path`); in `lifespan`, start only the last flagged slug and clear the rest, or refuse to boot with two flagged. Show the reason on the displaced event's tracking panel.
- Effort: S

### TR3-4: Non-string JSON values reach `.strip()` in several callees and become 500s instead of 400s
- Severity: Low
- File: web.py:1713 (`set_station_status`: `(body.get("changed_by") or "").strip()`), leaders.py:190 (`record_sighting`: `(bib or "").strip()`, `(by or "").strip()`), leaders.py:154 (`set_bib_color`: `(name or "").strip()`), styling.py:57 (`value.strip()` on `bib_color`), db.py:553 (`exclude_station`: `(reason or "").strip()`)
- What: The routes catch `ValueError`/`TypeError` (and `IncidentError`) but a JSON number or object in `bib`, `changed_by`, `bib_color`, `bib_color_name` or `reason` raises `AttributeError`, which is not caught, so FastAPI returns a 500 with a traceback in the log. `set_station_status` additionally uses `request.json()` directly (web.py:1707-1711) rather than `_json_body`, so a JSON array body raises `AttributeError` on `body.get`. The shipped client always sends strings, so this is API-hygiene only.
- Evidence: leaders.py:190 `(bib or "").strip()[:16] or None` with `bib=body.get("bib")` passed raw from web.py:1985.
- Why it matters: A 500 is logged as a server fault and hides the real cause; incidents.py already has `_clean()` that `str()`s first and is the pattern to reuse.
- Fix: Route the free-text fields through one `_text(value, limit)` helper that does `str(value)` before `.strip()`, and switch `set_station_status` to `_json_body`.
- Effort: S

### TR3-5: `undo_last_sighting` compares `division` un-normalised while `record_sighting` and `clear_sightings` lower-case it
- Severity: Low
- File: leaders.py:169 (record: `.strip().lower()`), leaders.py:229 (clear: `.strip().lower()`), leaders.py:201-205 (undo: raw `division`), web.py:2003-2006
- What: The three sibling functions disagree on normalisation. Division keys are produced by `categories.slugify` and are always lower-case, so the shipped client cannot hit it; an API caller sending `"Male"` gets a sighting recorded under `male` and an undo that silently returns `{"removed": false}`.
- Evidence: leaders.py:204 `(event_id, course_id, division)`.
- Why it matters: Latent only; listed so the next edit to one of the three does not widen it.
- Fix: Normalise once in the route (or in all three callees identically).
- Effort: S

### TR3-6: `record_sighting` accepts any division string, so a sighting can be stored under a key `for_event` will never display
- Severity: Low
- File: leaders.py:159-194, leaders.py:280-282 (divisions come from `lead_division_labels`)
- What: `record_sighting` validates `course_id` and `poi_id` against the event but not `division` against `lead_division`. A client holding a stale `divisions` list (a leader deleted in setup since the page loaded - deletion is refused only when that division already has sightings, categories.py:558) posts a sighting that is inserted and then invisible on every panel, with a 201 to say it worked.
- Evidence: leaders.py:169-171 is the only check: non-empty.
- Why it matters: The same "stored and then displayed as nothing" shape the CLAUDE.md leaders note describes for stations; resync after the setup change makes the window small.
- Fix: `if division not in categories.lead_division_labels(conn, event_id): raise ValueError(...)` in `record_sighting`.
- Effort: S

### TR3-7: `position` socket messages carry `label`/`category` that nothing reads, from a roster snapshot that goes stale
- Severity: Low
- File: web.py:137-144 (`roster_by_key.get(report.station_key)`), web.py:2126-2128, hub.py:105-107, app.js:2556-2559
- What: `roster_by_key` is built once per ingest task keyed by roster `station_key`, so a bare-callsign entry bound to `WX0MIK-5` or a cross-callsign bind made mid-event never matches and the fields are absent; when they are present they reflect the roster as of feed start. The client joins positions to the roster via `tracking_key` from `/state` and never reads `message.label` or `message.category`. The `/state` `positions[]` entries do not carry these keys either, so the snapshot and socket shapes differ for no consumer.
- Evidence: hub.py:105-107 sets them; `grep` of app.js finds no reader.
- Why it matters: Cleanliness, and a trap for the next person who tries to use them.
- Fix: Drop `roster_row` from `position_message` and the `roster_by_key` argument from `make_position_handler`, or resolve it per packet from a fresh `Membership`-style read.
- Effort: S

### TR3-8: The first packet after a roster change (adopt / add / unignore) is discarded rather than stored
- Severity: Low
- File: ingest.py:100-104 and 223-228
- What: `handle_line` decides `known` from the stale membership and returns `None` for the packet; only then does the loop refresh membership. The refreshed set is used for the "is it still news" check but the packet itself is gone - not stored, not fanned out. The next beacon (1-5 minutes) is stored normally.
- Evidence: ingest.py:218-222 stores before ingest.py:226 refreshes.
- Why it matters: One beacon interval of lag after NCS matches a station, which the docs already accept for a restart but not for a match; listed as latent.
- Fix: When `nearby` is non-empty and the refresh shows the key is now known, re-run `handle_line` for that line (or refresh before the call, rate-limited as now).
- Effort: S

### TR3-9: `_ssid_alerts` pairs `MAX(symbol_table)` with `MAX(symbol_code)` from different packets
- Severity: Low
- File: db.py:604-616 (`unexpected_ssids`), web.py:206-210
- What: The two columns are aggregated independently, so a station that beaconed `/#` then `\&` reports table `\` with code `#`, and `symbols.describe`/`is_infrastructure` describe a pair no packet carried. The domain rule says table and code travel together. (SQL body - flagged here because the callee's return is what `_ssid_alerts` hands to the UI; the SQL agent may already have it.)
- Evidence: db.py:608-609 `MAX(p.symbol_table) AS symbol_table, MAX(p.symbol_code) AS symbol_code`.
- Why it matters: `looks_like_infrastructure` is what tells NCS whether to adopt or dismiss; in practice a station rarely changes symbol mid-event, so Low.
- Fix: Take both from the newest row per station (join on `MAX(id)` as `latest_position_per_station` does).
- Effort: S

## Unconfirmed
none

## Clean
- `access.resolve` / `require_access` / `require_capability`: every route in range names a capability or is explicitly read-all; invalid token -> 404, valid-but-lacking -> 403; WebSocket resolves the same way and subscribes with the role's capabilities (web.py:2062-2072). No route reaches a module function with an `event_id` other than `granted.event_id`.
- Every db/incidents/leaders callee reached from this range takes and uses `event_id` in its WHERE clause; the only non-scoped callee, `incidents.log_for(conn, incident_id)`, is guarded by `incidents.get(conn, event_id, incident_id)` immediately before it (web.py:1946-1950).
- `hub.publish(..., requires=CAP_SSID)` for `nearby` and `requires=CAP_INCIDENT_REPORT` for every `incident` message match the `/state` filtering (web.py:1631-1636), so Staff gets neither on either path.
- JSON-serialisability: every payload is built from `sqlite3.Row` values (str/int/float/None), `PositionReport` (str/float/None), `CoursePosition.as_dict()`, `Leader.as_dict()`, `Incident.as_dict()`. No datetime, set or Row reaches `JSONResponse`/`send_json`.
- Arity and keyword names: all 47 module calls in the appendix match the callee signature; no positional/keyword swap, no ignored argument except the dead `roster_row` in TR3-7.
- Exceptions: `db.set_op_status`, `db.change_station_key`, `leaders.*` raise `ValueError`; `incidents.*` raise `IncidentError(ValueError)`; each route catches the right one. `int(None)` / `float(None)` -> `TypeError` is caught where `int()`/`float()` is used. Only the `.strip()` cases in TR3-4 are uncaught.
- `db.connect` is `isolation_level=None` (autocommit), so the `conn.close()`-without-commit pattern in every write route is correct.
- Middleware `publish_setup_changes`: regex `^/api/setup/events/(\d+)(?:/|$)` matches every event-scoped POST including `/delete` and `/tracking`; the extra resyncs are harmless. Non-event POSTs (`/api/setup/events`, users, orgs) correctly do not match.
- `lifespan` start/stop is symmetric; `_stop_ingest` handles the task's own `finally` pop; `_start_ingest` is idempotent for the same slug.
- `/help`: `guides.load` refuses anything not matching `^[a-z0-9][a-z0-9-]*$` except `README`; `/help/images` mount is declared after the routes as the comment says.
- `manifest`, `map_page`, `/` : trivial, correct.
- `/state` sets `payload["role"]` twice (web.py:1621, 1623) - harmless duplicate, not worth an ID.
- `build_state` calls `index.locate` twice per POI (once in `order_along_course`, once for `course_position`) and `_publish_incident` rebuilds `CourseIndex` per write - measurable only at scale, not reported.

## Section 3: authoritative server-side message and snapshot shapes

### `GET /api/{slug}/{token}/state` (web.py:223-381, 1616-1652) - `type: "state"`
Top-level keys: `type`, `event{slug,name,timezone,center_lat,center_lon,zoom}`, `courses[]`, `role_labels{key:name}`, `poi_categories[]`, `pois[]`, `roster[]`, `positions[]`, `ssid_alerts[]`, `leaders[]`, `divisions[]`, `incidents[]`*, `incident_statuses[]`, `incident_kinds[]`, `pickups_waiting`*, `op_statuses[]`, `thresholds{stale_after_s,silent_after_s}`, then added by the route: `role`, `role_label`, `can_write`, `capabilities[]`, `nearby[]`**, `ignored[]`**.
\* removed unless `CAP_INCIDENT_REPORT` (popped, not emptied). \*\* present only with `CAP_SSID`.
- `courses[]`: `id,name,color,dash_pattern,distance_m,sort_order,geojson` (note: NOT `bib_color`/`bib_color_name`; those ride on `leaders[]`).
- `poi_categories[]`: `key,name,staffed(bool),icon,color,visible(bool),show_labels(bool)`.
- `pois[]`: poi row `id,event_id,name,poi_type,lat,lon,what3words,label,sort_order,notes` + `course_position` (dict|null) + `label_text`.
- `roster[]`: roster row `id,event_id,station_key,bound_key,operator_name,display_label,category,expects_aprs,poi_id,op_status,op_status_at,op_status_by,color` + `op_status_label`, `tracking_key`; `course_position`, `poi_name` only when posted at a POI (absent otherwise, not null).
- `positions[]`: `station_key,received_at,lat,lon,course_deg,speed_kmh,altitude_m,symbol_table,symbol_code,comment,course_position`.
- `ssid_alerts[]`: `station_key,packets,last_at,symbol,looks_like_infrastructure,roster_candidates[{station_key,display_label,category}]`.
- `leaders[]` (leaders.py:98-118): `course_id,course_name,bib_color,bib_color_name,division,division_label,last_poi_id,last_poi_name,last_distance_m,last_at,last_by,bib,pace_mps,next_poi_id,next_poi_name,next_distance_m,eta_seconds`.
- `divisions[]`: `{value,label}`. `incident_statuses[]`, `incident_kinds[]`: `{value,label}`.
- `incidents[]`: incident row `id,event_id,bib,kind,status,lat,lon,poi_id,note,assigned_to,reported_at,reported_by,status_at,status_by,closed_at` + `status_label`, `kind_label` + `course_position`.
- `nearby[]` (web.py:172-181): `station_key,received_at,lat,lon,symbol,looks_like_infrastructure,packets,course_position`.
- `ignored[]`: station_exclusion row `id,event_id,station_key,reason,added_at`.
- `course_position` everywhere (progress.py:57-66): `course_id,course_name,distance_along_m,remaining_m,course_length_m,offset_m,fraction`.

### Socket messages (`hub.publish`)
| type | built at | keys | gated |
|---|---|---|---|
| `position` | hub.py:86-111 via web.py:137-144 | `type,station_key,received_at,lat,lon,course_deg,speed_kmh,altitude_m,symbol_table,symbol_code,comment,course_position` + optional `label,category` (TR3-7) | none |
| `resync` | web.py:148, 455, 1747 | `type` only | none |
| `nearby` | web.py:172-189 | `type` + the `nearby[]` entry keys above | `CAP_SSID` |
| `station_status` | web.py:1725-1732 | `type,station_key,op_status,op_status_at,op_status_by,op_status_label` | none |
| `incident` | web.py:1749-1765 | incident `as_dict()` keys + `course_position` + `type` + `change` in {`created`,`status`,`deleted`,`edited`} | `CAP_INCIDENT_REPORT` |
| `leaders` | web.py:1957-1967 | `type, leaders[]` (same entry shape as snapshot) | none |

Shape differences between snapshot and socket for the same data:
- Incident: snapshot entry = as_dict + `course_position`; socket = the same + `type`,`change`; the HTTP 201/200 body of create/status/update (web.py:1786, 1805, 1842) = as_dict WITHOUT `course_position`.
- Position: snapshot entries never carry `label`/`category`; socket ones sometimes do (TR3-7). Both use `received_at` as ISO string.
- Station status: socket carries a 5-field subset of the roster entry; `station_key` is the ROSTER key (post `resolve_station_key`), not `tracking_key`.
- Nearby: identical shape (the socket message is literally `{"type": "nearby", **entry}`).
- Leaders: identical shape.

## Appendix: route x module call

| route / handler | module call (def) | args OK? | return handled? | status |
|---|---|---|---|---|
| lifespan (391) | `db.connect(settings.db_path)` (db.py:14) | yes | conn | OK |
| lifespan | `db.init_schema(conn)` (db.py:88) | yes | list ignored | OK |
| lifespan | `db.set_ingest_enabled(conn, slug:str, True)` (db.py:428) | yes | None | OK |
| lifespan | `db.events_wanting_ingest(conn)` (db.py:440) | yes | list[str] iterated | OK (TR3-3 semantics) |
| lifespan | `_start_ingest(slug)` / `_stop_ingest(slug)` (2157/2173) | yes | None | OK |
| middleware publish_setup_changes (445) | `hub.publish(int(id), {"type":"resync"})` (hub.py:65) | yes | None | OK |
| make_position_handler.on_position (136) | `hub_module.position_message(report, roster_row, course_position)` (hub.py:86) | yes; `roster_row` stale/dead | dict published | MISMATCH (TR3-7, Low) |
| on_position | `_course_position(index, report.lat, report.lon)` -> `CourseIndex.locate(lat, lon)` (progress.py:165) | yes (floats) | dict|None | OK |
| on_position | `hub.publish(event_id:int, message)` | yes | None | OK |
| make_nearby_handler.on_nearby (168) | `index.locate(report.lat, report.lon)` | yes | as_dict/None | OK |
| on_nearby | `symbols.describe(table, code)`, `symbols.is_infrastructure(table, code)` (symbols.py:57/68) | yes (str|None) | str / bool | OK |
| on_nearby | `hub.publish(event_id, {...}, requires=access.CAP_SSID)` | yes | None | OK |
| _ssid_alerts (193) | `db.unexpected_ssids(conn, event_id)` (db.py:586) | yes | rows: station_key,packets,last_at,symbol_table,symbol_code | OK (TR3-9 on pairing) |
| _ssid_alerts | `db.roster_entries_for_base(conn, event_id, base:str)` (db.py:631) | yes | rows; reads station_key,display_label,category | OK |
| _ssid_alerts | `symbols.describe` / `is_infrastructure` | yes | str/bool | OK |
| build_state (223) | `progress.CourseIndex.for_event(conn, event_id)` (progress.py:97) | yes | CourseIndex | OK |
| build_state | `index.order_along_course(poi_rows)` (progress.py:133) | yes; rows have sort_order,lat,lon | sorted rows | OK |
| build_state | `poi_labels.for_poi(row["name"], row["label"])` (labels.py:87) | yes (str, str|None) | str | OK |
| build_state | `db.op_status_label(category, op_status)` (db.py:449) | yes | str | OK |
| build_state | `db.tracking_key(row)` (db.py:224) | yes (Row) | str | OK |
| build_state | `db.excluded_station_keys(conn, event_id)` (db.py:534) | yes | set[str] | OK |
| build_state | `db.latest_position_per_station(conn, event_id)` (db.py:390) | yes | rows, all read keys exist | OK |
| build_state | `incidents.for_event(conn, event_id)` (incidents.py:263) | yes (include_closed default True) | rows -> `Incident(row).as_dict()` | OK |
| build_state | `categories.role_labels(conn, event_id)` (categories.py:353) | yes | dict | OK |
| build_state | `categories.poi_categories(conn, event_id)` (categories.py:149) | yes | rows; keys read exist | OK |
| build_state | `leaders.for_event(conn, event_id, index)` (leaders.py:262) | yes (divisions default) | list[Leader] -> as_dict | OK |
| build_state | `categories.lead_divisions(conn, event_id)` (categories.py:480) | yes | rows key,name | OK |
| build_state | `incidents.waiting_count(conn, event_id)` (incidents.py:283) | yes | int | OK |
| GET / (1552) | none | - | - | OK |
| GET /e/{slug}/{token} (1560) | `access.resolve(conn, slug, token)` via require_access (access.py:242) | yes | Access|None -> 404 | OK |
| GET manifest (1571) | require_access; raw SQL on event | yes | name read | OK |
| GET /state (1616) | require_access; `build_state(conn, event_id)`; `granted.can(cap)`; `_nearby_for(event_id)`; `db.exclusions(conn, event_id)` (db.py:566) | yes | dict; rows->dict | OK |
| _nearby_for (1654) | `db.all_station_keys` (db.py:726), `db.bound_station_keys` (319), `db.excluded_station_keys` (534) | yes | list/set/set | OK |
| require_capability (1689) | `granted.can(capability)` (access.py:119) | yes | bool -> 403 | OK |
| POST station/{key}/status (1703) | `db.set_op_status(conn, event_id, station_key:str, op_status:str, changed_by:str|None)` (db.py:455) | yes; body not via `_json_body` | Row; ValueError->400 | OK (TR3-4 for non-str `changed_by`) |
| same | `db.op_status_label(row["category"], row["op_status"])` | yes | str | OK |
| same | `hub.publish(event_id, payload)` | yes | - | OK |
| _publish_incident (1749) | `CourseIndex.for_event`; `incidents.Incident(row).as_dict()`; `_course_position`; `hub.publish(..., requires=CAP_INCIDENT_REPORT)` | yes | - | OK |
| POST /incidents (1767) | `incidents.create(conn, event_id, lat=float, lon=float, bib=raw, note=raw, poi_id=raw, by=raw, kind=str)` (incidents.py:110) | yes; `_clean` str()s bib/note/by; poi_id raw is compared in SQL (affinity converts) | Row; IncidentError/TypeError/ValueError->400 | OK |
| POST /incidents/{id}/status (1788) | `incidents.set_status(conn, event_id, incident_id:int, status:str, by=raw)` (152) | yes | Row; IncidentError->400 (missing id also 400, not 404) | OK |
| POST /incidents/{id}/delete (1807) | `incidents.delete(conn, event_id, incident_id)` (236) | yes | Row (pre-delete); ->404 | OK |
| POST /incidents/{id} (1824) | `incidents.update(conn, event_id, incident_id, by=raw, **{bib,note,assigned_to,lat,lon})` (186) | yes; lat without lon silently ignored; `assigned_to` writable by CAP_INCIDENT_REPORT roles | Row; ->400 | OK (note on `assigned_to` scope) |
| POST /ssid/adopt (1844) | `db.change_station_key(conn, event_id, old:str, new:str)` (db.py:641) | yes | Row station_key,display_label; ValueError->400 | OK |
| same | `app.state.nearby[...].pop(upper)`; `_publish_state_hint` | yes | - | OK |
| POST /ssid/unbind (1866) | raw roster SELECT; `db.unbind_station(conn, event_id, key)` (307) | yes | None; row read before | OK |
| POST /ssid/ignore (1886) | `db.exclude_station(conn, event_id, key:str, reason:raw)` (545) | yes (TR3-4 for non-str reason) | None | OK |
| POST /ssid/unignore (1902) | `db.unexclude_station(conn, event_id, key)` (556) | yes | bool -> 404 if False | OK |
| GET /station-log (1918) | `db.op_status_log(conn, event_id, station_key:str|None)` (711) | yes | rows->dict | OK |
| GET /incidents/{id}/log (1937) | `incidents.get(conn, event_id, id)` (226); `incidents.log_for(conn, id)` (299) | yes; log_for unscoped but guarded | rows->dict; ->404 | OK |
| _publish_leaders (1957) | `CourseIndex.for_event`; `leaders.for_event(conn, event_id, index)`; `hub.publish` | yes | - | OK |
| POST /leaders/sighting (1969) | `leaders.record_sighting(conn, event_id, course_id=int, division=str, poi_id=int, bib=raw, by=raw)` (159) | yes (TR3-4 raw bib/by; TR3-6 division unvalidated) | Row ignored; ValueError/TypeError->400 | OK |
| POST /leaders/undo (1996) | `leaders.undo_last_sighting(conn, event_id, int, str)` (197) | yes (TR3-5 casing) | bool | OK |
| POST /leaders/reset (2014) | `leaders.clear_sightings(conn, event_id, int, str)` (212) | yes | int | OK |
| POST /course/{id}/bib-color (2036) | `leaders.set_bib_color(conn, event_id, course_id:int, color:raw, name:raw)` (132) -> `styling.is_valid_color/normalize_color` | yes (TR3-4 non-str) | Row id,bib_color,bib_color_name; ValueError->400 | OK |
| WS /ws/{slug}/{token} (2061) | `db.connect`; `access.resolve`; `hub.subscribe(event_id, capabilities)` (hub.py:52); `hub.unsubscribe(sub)` (59) | yes | Subscription; queue.get -> send_json | OK |
| GET /help, /help/{page} (2091) | `guides.load(name)` (guides.py:88); `guides.page_html(page, guides.page_names())` (318, 73) | yes | Page|None -> 404; str | OK |
| _ingest_for (2118) | `db.get_event(conn, slug)` (187); `db.roster_for_event` (328); `db.all_station_keys`; `db.bound_station_keys`; `CourseIndex.for_event` | yes | Row|None handled | OK |
| _ingest_for | `run_ingest(settings, slug, on_position=, on_nearby=)` (ingest.py:164) | yes | IngestStats ignored; raises SystemExit | MISMATCH (TR3-1) |
| _supervise_ingest (2142) | catches CancelledError, Exception | - | SystemExit escapes | MISMATCH (TR3-1) |
| _start_ingest / _stop_ingest | asyncio only | yes | - | OK (TR3-3 persistence) |
