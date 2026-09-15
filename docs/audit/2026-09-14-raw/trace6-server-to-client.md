# Server-to-client traceability audit (TR6)

## Summary
Scope: the return direction - every payload the server hands a browser. (A) the live map: `build_state` / `GET /api/{slug}/{token}/state` and every `hub.publish` (web.py, hub.py, ingest.py) against `applyState` and the `onmessage` dispatcher in `static/app.js`, plus the connection lifecycle; (B) every `/api/setup/*` JSON response (web.py, admin.py, users.py, categories.py) against what `static/setup.js` reads, plus the report page's embedded JSON. Read in full: web.py, hub.py, ingest.py, app.js, setup.js; relevant parts of admin.py, db.py, incidents.py, leaders.py, progress.py, categories.py, users.py, symbols.py, report.py, schema.sql, and the git history for two regressions.
Counts: 3 High, 6 Medium, 8 Low. No Critical. Privacy gating (nearby / ignored to CAP_SSID, incidents / pickups_waiting to CAP_INCIDENT_REPORT, left-out-not-empty) is honoured; the one leak-adjacent item (ssid_alerts to every role) is Low.

## Findings

### TR6-1: `station_status` socket message is keyed by the roster key, but the client's roster map is keyed by the tracking key
- Severity: High
- File: src/courseops/web.py:1727-1733; src/courseops/static/app.js:2472, 2545-2553; src/courseops/db.py:224-236, 455-469
- What: The status broadcast carries `row["station_key"]` (what a human typed on the roster). `applyState` keys `state.roster` by `r.tracking_key || r.station_key`, i.e. the bound SSID for a bare-callsign entry. So for any roster entry that has a `bound_key`, every OTHER browser's `state.roster.get(message.station_key)` misses and the update is silently dropped. The originating browser looks right because it updated optimistically.
- Evidence: `"station_key": row["station_key"],` (web.py:1729) vs `state.roster = new Map(data.roster.map((r) => [r.tracking_key || r.station_key, r]));` (app.js:2472) and `const entry = state.roster.get(message.station_key); if (entry) {...}` (app.js:2546-2547).
- Why it matters: Bare-callsign roster entries are a supported, documented path ("A roster entry may be a bare callsign; `bound_key` is the SSID heard"). With three NCS operators on three screens, one marks "Aid 3 - torn down"; the other two keep showing "On station" until a resync happens to arrive. The status log is right, the screens disagree, nobody gets an error.
- Fix: Add `"tracking_key": db.tracking_key(row)` to the `station_status` payload and look up `state.roster.get(message.tracking_key || message.station_key)` on the client (or fall back to scanning for `entry.station_key === message.station_key`). Add a test that a status set on a bound entry publishes the tracking key.
- Effort: S

### TR6-2: A failed snapshot fetch during reconnect ends reconnection for good
- Severity: High
- File: src/courseops/static/app.js:2376-2387, 2575-2586, 2785-2787
- What: `loadState()` has no try/catch around `fetch`. `scheduleReconnect` does `await loadState(); connect();` inside a `setTimeout` async callback. When the socket drops BECAUSE the phone is in a dead zone, the retry fires 1 s later while still offline, `fetch` rejects (TypeError), the rejection is unhandled, `connect()` never runs and nothing schedules another attempt. The badge reads "Connecting..." until someone reloads the page. The initial `if (await loadState()) connect();` has the same shape: a transient failure on first load leaves a page that never connects.
- Evidence: `setTimeout(async () => { setConnection('connecting', 'Connecting…'); await loadState(); connect(); }, delay);` (app.js:2578-2583); `const response = await fetch(...)` with no handler (app.js:2378).
- Why it matters: The dead zone is the exact scenario the reconnect exists for ("a phone back from a dead zone must not show a stale picture"). A SAG driver coming back into coverage keeps a map frozen at the moment they lost signal, with a badge that looks like it is about to recover. iOS may or may not reload the tab on its own; nothing in the app will.
- Fix: Wrap the body of the reconnect callback in try/finally so `connect()` (or another `scheduleReconnect()`) always runs; have `loadState` catch a rejected fetch and return false. Consider connecting the socket first and loading state on `open` (see TR6-8), which also removes the ordering gap.
- Effort: S

### TR6-3: Places CSV export writes an empty Coordinates column
- Severity: High
- File: src/courseops/static/setup.js:2108-2117 vs 1130-1142
- What: The export reads `tr.querySelector('.coords span')`. The coordinates cell stopped containing a `<span>` when #108 replaced it with two `<input>` boxes and a copy button (commit history: the span was introduced in #62 alongside the export; the cell was rewritten later). The selector now matches nothing and the column is always "".
- Evidence: `(tr.querySelector('.coords span') || {}).textContent || '',` (setup.js:2114); the cell is now `<input value="${p.lat.toFixed(5)}" data-plat=...><input ... data-plon=...>${iconBtn('copy', ...)}` (setup.js:1131-1140) - no span.
- Why it matters: The CSV is "what gets shared with the organizer and read from on air" (comment at 2096-2100): the list of stops with their coordinates and what3words. It now ships layer, name and words with no position.
- Fix: Read `[data-plat]` / `[data-plon]` values from the row (unsaved edits included, which is what the comment promises) and join them as `lat, lon`. Add a DOM-level check to the export or a test on the row markup.
- Effort: S

### TR6-4: A `resync` never re-renders the layer switches or the role names in them
- Severity: Medium
- File: src/courseops/static/app.js:2488 (and 1028-1080, 1083-1086)
- What: `applyState` updates `state.poiCategories` and `state.roleLabels` on every load, but calls `renderLayerToggles()` only when `firstLoad`. Every setup POST publishes a resync so the field sees the change, and the pins DO redraw with the new name/colour/glyph - but the panel's "Places" switches keep the old list: a layer added in setup has no switch, a deleted one keeps a dead switch, a renamed layer or station role shows its old name beside pins that show the new one.
- Evidence: `if (firstLoad) renderLayerToggles();` (app.js:2488).
- Why it matters: The resync middleware exists precisely so "a renamed station has to reach the field" without a reload. Here half of it does, and the half that doesn't is the list someone uses to turn a layer on - a new "Medics" layer added on race morning cannot be switched on from a phone without a full page reload.
- Fix: Call `renderLayerToggles()` on every `applyState` (it is idempotent and reads `state.layerPrefs`, so the viewer's own switches survive).
- Effort: S

### TR6-5: The "straight into the bib field" focus after dropping a pin targets attributes no element carries
- Severity: Medium
- File: src/courseops/static/app.js:1915-1923; regression from commit 6eeedb8 (#93)
- What: After a pickup/note is created the code focuses `[data-bib-for="<id>"]` / `[data-note-for="<id>"]`. #93 renamed those attributes to `data-edit-key="bib:<id>"` / `note:<id>` (its message even calls the old ones "unused"), so the selector never matches and the focus never happens. Separately, the row only exists once the `incident` socket message arrives; the 60 ms timer can fire before that on a slow link even when the selector is fixed.
- Evidence: `? \`[data-note-for="${created.id}"]\` : \`[data-bib-for="${created.id}"]\`;` (app.js:1920); the inputs are rendered with `bib.dataset.editKey = \`bib:${incident.id}\`` (app.js:2162) and `field.dataset.editKey = \`note:${incident.id}\`` (app.js:2295).
- Why it matters: "Create first, fill the bib in after" is the documented flow; this was the step that put the cursor where the bib goes. It has been silently dead since #93, so a SAG driver drops a pin and then has to find the row and tap the box, in a glove.
- Fix: Select by `[data-edit-key="bib:<id>"]` / `note:<id>`; on create, insert the POST response into `state.incidents` and render immediately (the socket message then updates the same row), so the field exists when the timer fires.
- Effort: S

### TR6-6: An ignored station's packets keep reaching every browser as `position` messages until the next unknown packet refreshes membership
- Severity: Medium
- File: src/courseops/ingest.py:82-88, 143-160, 219-227; src/courseops/static/app.js:2555-2559
- What: `handle_line` drops excluded stations using `membership.excluded`, which is only re-read inside `Membership.refresh()`, and `refresh()` is only called when a packet from an UNKNOWN station arrives. Ignoring a station whose base callsign is rostered (the usual case: the operator's own digipeater under `WX0MIK-*`) therefore takes effect at the door only after some unrelated unknown station is heard. Until then its packets are stored and published; the resync on Ignore hides the stored position in the snapshot, but the next beacon re-adds the marker on every screen, and the client applies `position` messages with no ignore filter.
- Evidence: `if excluded and report.station_key in excluded:` (ingest.py:85) against `self.excluded = db.excluded_station_keys(conn, event_id)` set only in `refresh` (ingest.py:160), called only under `if nearby:` (ingest.py:226). Client: `state.positions.set(message.station_key, message); upsertStationMarker(...)` (app.js:2557-2558), no check against `state.ignored`.
- Why it matters: NCS presses Ignore on the digipeater, it disappears, and ten minutes later it is back on the map at the operator's house - and now also written to the database. It looks like Ignore does not work, which is how people stop trusting the screen. On a quiet band the window can be long.
- Fix: Refresh membership when the feed's `stats.excluded`-relevant state changes: have the ignore/unignore/adopt endpoints force a refresh (the ingest runs in-process; store the `Membership` on `app.state` and call `refresh(force=True)`), or make `refresh()` also run on a short timer. As defence, the client should drop `position` messages for keys in `state.ignored` when it has that list.
- Effort: M

### TR6-7: The hub drops messages on a full queue with no recovery; the comment's "resyncs on reconnect" does not follow
- Severity: Medium
- File: src/courseops/hub.py:9-13, 73-83; src/courseops/web.py:2062-2078
- What: On `QueueFull` the message is discarded and `dropped` is counted; the socket stays open, so no reconnect and no resync follows. Dropping a `position` is harmless as the module says, but the same path drops `incident` (`change: "deleted"`), `station_status`, `leaders` and `resync` itself - none of which is superseded by a later message.
- Evidence: `except asyncio.QueueFull: sub.dropped += 1` with only a log line (hub.py:77-83); the WebSocket loop just `await subscription.queue.get()` (web.py:2075).
- Why it matters: A stalled browser that recovers (a throttled background tab, a phone that slept with the socket alive) keeps a deleted pickup in its queue and on its map forever, or misses the rename everyone else got, with no signal on screen.
- Fix: When a drop happens, enqueue nothing more until the queue drains and then push one `{"type": "resync"}` (a flag on the Subscription: `needs_resync`); the client already knows how to handle it.
- Effort: S

### TR6-8: Snapshot is fetched BEFORE the socket subscribes, so anything published in between is lost
- Severity: Medium
- File: src/courseops/static/app.js:2578-2583, 2785-2787; src/courseops/web.py:2062-2078
- What: Both first load and every reconnect do `await loadState()` and only then `connect()`. The server sends nothing on socket open. A status change, sighting or incident published in the gap (snapshot served -> subscription registered; seconds on a slow phone) is never seen by that browser until an unrelated resync arrives.
- Evidence: `await loadState(); connect();` (app.js:2582-2583); `if (await loadState()) connect();` (app.js:2786); the WebSocket handler subscribes and then only forwards the queue (web.py:2070-2075).
- Why it matters: Reconnects happen most on race morning on phones with poor coverage, which is also when statuses change every few seconds. A missed `station_status` or `incident` is a wrong row with no indication.
- Fix: Open the socket first, subscribe, then fetch the snapshot on `open` (messages queued in the meantime are applied after the snapshot and are idempotent). Or have the server push a `resync` as the first message after `accept()`.
- Effort: S

### TR6-9: The SSID panel is rebuilt on every `nearby` packet with no field-edit capture
- Severity: Medium
- File: src/courseops/static/app.js:2561-2565, 1312-1437
- What: Every `nearby` message calls `renderSsidAlerts()`, which does `host.innerHTML = ''` and rebuilds every card including the "This is..." `<select>`. Unlike `renderLeaders`/`renderIncidents`, it does not go through `captureFieldEdit`/`restoreFieldEdit`, and a `<select>` would not be restored by them anyway (no `data-edit-key`, and the dropdown closes when the element is replaced). With an area filter around a course in a town, unknown stations beacon continuously.
- Evidence: `state.nearby.set(message.station_key, message); renderSsidAlerts();` (app.js:2563-2564); `host.innerHTML = '';` (app.js:1355) with no capture.
- Why it matters: NCS is scrolling a thirty-entry list to match a borrowed rig to the person using it and the dropdown snaps shut under their thumb every few seconds. CLAUDE.md names this exact class of bug for the bib field.
- Fix: Give the select `data-edit-key="ssidpick:<station_key>"`, make `restoreFieldEdit` tolerate selects (value only), and skip the rebuild while a select inside the panel has focus (or coalesce nearby renders with a short debounce).
- Effort: S

### TR6-10: Any non-2xx snapshot reads as "Access denied" / "Not available"
- Severity: Low
- File: src/courseops/static/app.js:2379-2383
- What: `loadState` maps every `!response.ok` - including the 502 Apache returns during a deploy restart - to "Access denied" and replaces the event name with "Not available". It recovers on the next successful load, but the text is wrong meanwhile and the same message is shown for a genuinely revoked link.
- Evidence: `if (!response.ok) { setConnection('down', 'Access denied'); document.getElementById('event-name').textContent = 'Not available'; return false; }`
- Why it matters: A volunteer seeing "Access denied" during a 20-second deploy will ask for a new link. Only a 404 means the link is dead.
- Fix: Say "Access denied" on 404/403 only; anything else "Server unavailable - retrying".
- Effort: S

### TR6-11: `ssid_alerts` are sent to every role; only NCS renders them
- Severity: Low
- File: src/courseops/web.py:355, 1616-1647; src/courseops/static/app.js:1319-1323
- What: `build_state` always includes `ssid_alerts` (unrostered SSIDs under rostered callsigns, packet counts, last-heard, symbol, roster candidates) and `state()` never strips it for roles without `CAP_SSID`; `renderSsidAlerts` returns early unless `can('ssid')`. Not a new position leak - those stations' latest positions already go to every role in `positions` by the wildcard rule - but it is roster-adjacent data handed to Staff, the forwarded link, for nothing.
- Evidence: `"ssid_alerts": _ssid_alerts(conn, event_id),` (web.py:355) with no `payload.pop` for it (web.py:1631-1647); `if (!can('ssid')) { section.hidden = true; ... return; }` (app.js:1319-1323).
- Why it matters: Waste, and one more field to remember if the alerts ever grow a comment or a location. The rule is "LEFT OUT rather than sent empty" for anything role-gated.
- Fix: Pop `ssid_alerts` alongside `nearby`/`ignored` gating in `state()` (send only when `granted.can(CAP_SSID)`); extend the Staff snapshot test at tests/test_web.py:564 to assert it is absent.
- Effort: S

### TR6-12: Keys sent that no client reads
- Severity: Low
- File: src/courseops/web.py:266-269, 351-355, 366, 371; src/courseops/hub.py:104-106; src/courseops/web.py:829, 999, admin.py:54-69
- What: Live map snapshot: `pickups_waiting` (client computes `live` itself, same definition), `divisions` (stored in `state.divisions`, never read - leaders already carry `division_label`), `incident_kinds` (stored, never read), `roster[].poi_name`, `roster[].color`, `roster[].id/event_id`. Socket `position`: `label` and `category` are never read (the client uses the roster), and are also built from `roster_by_key` snapshotted at feed start and keyed by roster key, so they would be stale/missing for bound entries if anyone started reading them. Setup: `GET .../roster` sends `ignored` that setup.js never shows; `POST .../import` returns `features` immediately discarded because `importFiles` calls `loadStaged()` anyway; `events[].counts.pending_imports` unread.
- Evidence: grep of `pickups_waiting|state.divisions|incidentKinds|poi_name` in app.js shows assignment only; `data.ignored` never referenced in setup.js.
- Why it matters: Payload and code that looks load-bearing and is not; `divisions` in particular is the field CLAUDE.md says was "copied into the state endpoint and again into app.js" - the app.js half is now dead.
- Fix: Drop `label`/`category` from `position_message` (or key them by tracking key and use them); remove `pickups_waiting`, `divisions`, `incident_kinds` from the snapshot or start reading them; drop `ignored` from the roster GET or render it.
- Effort: S

### TR6-13: Endpoints no client calls
- Severity: Low
- File: src/courseops/web.py:1918-1935 (`station-log`), 1937-1955 (`incidents/{id}/log`), 2036-2060 (`course/{id}/bib-color`)
- What: Neither app.js nor setup.js fetches these. They are tested and harmless, but `bib-color` is a live-app write capability (`CAP_COURSE`) with no UI behind it - bib colours are set in setup, which publishes a resync instead.
- Evidence: grep for `station-log`, `/log`, `bib-color` across `static/` returns only the app.js comment-free absence (no matches).
- Why it matters: Dead API surface, and `CAP_COURSE` appears in the capability fallback list in app.js:2396-2397 for a control that does not exist.
- Fix: Either wire a "history" view (shift handover is the stated use) or remove/mark the routes as CLI-only; drop `CAP_COURSE` from the live route if setup owns bib colours.
- Effort: S

### TR6-14: Link `last_used` is shown as a raw UTC ISO string
- Severity: Low
- File: src/courseops/static/setup.js:2286-2287
- What: `'Last used ' + esc(l.last_used)` prints `2026-09-14T13:02:11Z`. Everywhere else a stored time is formatted by the browser in the event's zone.
- Evidence: `<p class="muted">${l.last_used ? 'Last used ' + esc(l.last_used) : 'Never used'}</p>`
- Why it matters: The officer deciding which of three NCS links to revoke reads "last used 13:02Z" and has to convert.
- Fix: Format with `Intl.DateTimeFormat` in the event's `timezone` (available from `S.events`), the way the report page does.
- Effort: S

### TR6-15: Courses table still saves one row at a time and reloads the whole table
- Severity: Low
- File: src/courseops/static/setup.js:1046-1058
- What: `[data-savec]` posts one course and then calls `loadCourses()`, which re-renders both the courses and the places tables - discarding edits in progress in every other row and in the Places table below it. This is the pattern CLAUDE.md says was removed everywhere ("Do not go back to a save button per row"). Out of this audit's strict scope (client-only), noted because it was confirmed while tracing the response.
- Evidence: `await post(\`/api/setup/events/${S.eventId}/courses/${id}\`, {...}); banner('Course saved.'); loadCourses();`
- Why it matters: Three courses, so usually one row - but a half-edited Places table below is lost by pressing Save on a course colour.
- Fix: Wire the courses table to `bindSaveAll` like the others.
- Effort: S

### TR6-16: Every SSID action loads the snapshot twice
- Severity: Low
- File: src/courseops/static/app.js:1307; src/courseops/web.py:1741-1747, 1862, 1883, 1899, 1915
- What: `resolveSsid` calls `loadState()` after the POST, and the server also publishes `{"type": "resync"}` for the same action, which the same browser receives and acts on.
- Evidence: `await loadState(); // the roster changed; resync rather than patch` and `await _publish_state_hint(granted.event_id)` in each ssid endpoint.
- Why it matters: Two full snapshots per tap on the busiest panel NCS uses; harmless otherwise.
- Fix: Drop the client-side `loadState()` and rely on the resync (which reaches every browser anyway).
- Effort: S

### TR6-17: Event delete is offered only to system admins; the server allows org admins
- Severity: Low
- File: src/courseops/static/setup.js:552-553; src/courseops/web.py:789-801
- What: The Delete icon renders only when `S.user.is_system_admin`; the endpoint requires `user.may_create_events`, which an org admin has. Not a mismatch that breaks anything - it is stricter on the client - but the two disagree about who may do it.
- Evidence: `${S.user.is_system_admin ? iconBtn('remove', ...) : ''}` vs `if not user.may_create_events: raise HTTPException(403, ...)`.
- Why it matters: A club officer cannot remove their own rehearsal event without the host, although the server (and its comment: "a club must still be able to remove its own events") says they can.
- Fix: Gate the button on `S.user.may_create_events`.
- Effort: S

## Unconfirmed
none

## Clean
- Role gating of the snapshot: `incidents` and `pickups_waiting` are popped (not emptied) for roles without `CAP_INCIDENT_REPORT`; `nearby` and `ignored` added only for `CAP_SSID` (web.py:1631-1647). Socket: `incident` requires `CAP_INCIDENT_REPORT`, `nearby` requires `CAP_SSID`; `resync`, `leaders`, `station_status`, `position` go to all, and contain nothing a Staff link may not see.
- Every message type the server publishes has a handler: `position`, `nearby`, `resync`, `station_status`, `incident` (created/status/edited/deleted), `leaders`. No handler exists for a type nothing publishes. Snapshot `type: "state"` is never sent on the socket and is not dispatched on.
- Snapshot vs socket shapes agree for stations (`positions[]` == `position_message` minus `type/label/category`), incidents (`Incident.as_dict()` + `course_position` in both), leaders (`Leader.as_dict()` in both), nearby (same dict). No lat/lon vs latitude/longitude, number-as-string, or epoch-vs-ISO mismatches; every timestamp is `%Y-%m-%dT%H:%M:%SZ` and `ageSeconds`/`clockTime` tolerate a missing Z.
- Capability strings agree: `access.py` CAP_* values match every `can('...')` in app.js.
- `incidents.waiting_count` and the client's `live` count use the same definition (kind pickup, status not in dropped_off/closed); `INCIDENT_RANK` in app.js has all five server statuses.
- `captureFieldEdit` coverage: every text input in a socket-rendered list (leader bib, pickup bib, pickup note, course note) carries `data-edit-key`; the stations list has no inputs. The gap is the SSID select (TR6-9).
- Resync path: `loadState` re-fetches and re-applies event, courses, poi_categories, role_labels, pois, roster, positions, incidents, leaders, divisions, ssid_alerts, nearby, ignored; view/prefs restored on first load only, as documented. Every setup POST under `/api/setup/events/{id}` hits the middleware regex; non-event setup routes (orgs, users, session) change nothing the map shows. Gap is TR6-4.
- Backoff: 1 s doubling to 30 s, reset on `open`; `error` -> `close` -> one reconnect, no duplicate timers. `visibilitychange`/`pageshow` only restore the viewport, they do not reconnect (the socket close does).
- `escapeHtml` used on every server string interpolated into markup in app.js; `cssColor` guards colour into style; setup.js `esc` likewise. Report page JSON escapes `<`.
- Setup responses read correctly: session (user.*, first_run, version, build), events (+counts), organizations, staged (geojson parsed), import (filename/total/by_type/warnings), assign (warnings/distance_m), courses (geojson left as a string and `JSON.parse`d in `renderPlaceMap`), pois (all read keys present incl. `course_ids`, `label_auto`, `show_labels`), categories (place_count/in_use present), tracking (all nine keys read and sent), roster (+categories/pois), links (slug + token rows), users (roles/organizations/is_active/events). POST responses read by setup.js (`saved.name`, `created.name/id`, `result.moved`, tracking state) are all sent.
- Report page: `courses` JSON (`name`, `color`, `coordinates`) matches the inline script; note cards carry `data-lat`/`data-lon` and the script reads exactly those.

## Appendix A - socket message types

| type | published at | keys sent | keys read (app.js) | status |
|---|---|---|---|---|
| `position` | ingest -> `make_position_handler` (web.py:137) via `hub.position_message` (hub.py:87-113) | type, station_key, received_at, lat, lon, course_deg, speed_kmh, altitude_m, symbol_table, symbol_code, comment, course_position{course_id,course_name,distance_along_m,remaining_m,course_length_m,offset_m,fraction} \| null; label, category only when roster_by_key hit | station_key, received_at, lat, lon, speed_kmh, altitude_m, comment, course_position.{distance_along_m,course_name,remaining_m,offset_m} | OK; label/category unread (TR6-12); no ignore filter (TR6-6) |
| `resync` | first packet from unknown station (web.py:148); setup middleware on any `POST /api/setup/events/{id}...` (web.py:455); ssid adopt/unbind/ignore/unignore (web.py:1747) | type | type | OK; reload does not refresh layer toggles (TR6-4) |
| `nearby` (requires CAP_SSID) | `make_nearby_handler` (web.py:187) | type, station_key, received_at, lat, lon, symbol, looks_like_infrastructure, packets, course_position | station_key, received_at, symbol, packets, looks_like_infrastructure, course_position, roster_candidates (absent -> []) | OK; re-render without edit capture (TR6-9) |
| `station_status` | `set_station_status` (web.py:1737) | type, station_key, op_status, op_status_at, op_status_by, op_status_label | station_key (map lookup), op_status, op_status_at, op_status_by, op_status_label | key mismatch for bound entries (TR6-1) |
| `incident` (requires CAP_INCIDENT_REPORT) | `_publish_incident` (web.py:1761) with change=created/status/edited/deleted | Incident row: id, event_id, bib, kind, status, lat, lon, poi_id, note, assigned_to, reported_at, reported_by, status_at, status_by, closed_at + status_label, kind_label, course_position, type, change | id, change, kind, status, status_label, bib, note, assigned_to, lat, lon, reported_at, reported_by, status_at, course_position | OK |
| `leaders` | `_publish_leaders` (web.py:1967) after sighting/undo/reset/bib-color | type, leaders[]: course_id, course_name, bib_color, bib_color_name, division, division_label, last_poi_id, last_poi_name, last_distance_m, last_at, last_by, bib, pace_mps, next_poi_id, next_poi_name, next_distance_m, eta_seconds | all except last_by, last_distance_m, next_distance_m | OK |
| (on connect) | WebSocket handler (web.py:2062-2078) | nothing | - | gap: snapshot precedes subscribe (TR6-8) |

## Appendix A2 - snapshot `GET /api/{slug}/{token}/state` (web.py:223-378, 1616-1647)

| section | keys sent | keys read | status |
|---|---|---|---|
| top-level | type, role, role_label, can_write, capabilities, thresholds{stale_after_s,silent_after_s}, op_statuses, incident_statuses[{value,label}], incident_kinds[{value,label}], pickups_waiting*, divisions[{value,label}] | role, role_label, can_write, capabilities, thresholds, op_statuses, incident_statuses | incident_kinds, divisions, pickups_waiting unread (TR6-12) |
| event | slug, name, timezone, center_lat, center_lon, zoom | name, timezone, center_lat, center_lon, zoom | OK |
| courses[] | id, name, color, dash_pattern, distance_m, sort_order, geojson (parsed) | id, name, color, dash_pattern, distance_m, geojson.coordinates | OK |
| role_labels | {key: name} | key lookup | OK |
| poi_categories[] | key, name, staffed, icon, color, visible, show_labels | all | OK |
| pois[] | poi row (id, event_id, name, poi_type, lat, lon, what3words, label, sort_order, notes) + course_position, label_text | id, name, poi_type, lat, lon, what3words, notes, course_position, label_text | OK |
| roster[] | roster row (id, event_id, station_key, bound_key, operator_name, display_label, category, expects_aprs, poi_id, op_status, op_status_at, op_status_by, color) + op_status_label, tracking_key, course_position?, poi_name? | tracking_key, station_key, bound_key, display_label, category, expects_aprs, operator_name, poi_id, op_status, op_status_label, course_position | poi_name, color unread |
| positions[] (excludes ignored) | as `position` minus type/label/category | as above | OK |
| ssid_alerts[] | station_key, packets, last_at, symbol, looks_like_infrastructure, roster_candidates[{station_key,display_label,category}] | all except candidate.category | sent to all roles (TR6-11) |
| leaders[] | as `leaders` message | as above | OK |
| incidents[]* | Incident.as_dict + course_position | as `incident` | OK (popped for non-report roles) |
| nearby[]** | as `nearby` message minus type | as above | OK (CAP_SSID only) |
| ignored[]** | station_exclusion row: id, event_id, station_key, reason, added_at | station_key, reason, added_at | OK (CAP_SSID only) |

\* omitted unless CAP_INCIDENT_REPORT. \*\* present only with CAP_SSID.

## Appendix B - setup responses

| response | route | keys sent | keys read (setup.js) | status |
|---|---|---|---|---|
| session | GET /api/setup/session (web.py:617) | user{id,username,display_name,role,role_label,organization_id,is_system_admin,is_org_admin,may_manage_users,may_create_events} \| null, first_run, version, build ("" when signed out) | user.*, first_run, version, build | OK |
| first-user | POST /api/setup/first-user | user, created | none (message only) | OK |
| login | POST /api/setup/login | user | user | OK |
| logout / password | POST | ok | none | OK |
| events | GET /api/setup/events (web.py:739) | events[]: event row (id, organization_id, slug, name, event_date, timezone, center_lat, center_lon, zoom, aprs_filter_extra, is_active, ingest_enabled, created_at) + counts{courses,pois,roster,pending_imports}; organizations[] | id, name, slug, event_date, timezone, center_lat, center_lon, counts.courses/pois/roster; organizations | pending_imports unread (TR6-12) |
| event create/update | POST /api/setup/events, /events/{id} | event row | name, id | OK |
| event delete | POST /events/{id}/delete | deleted | none | UI gate stricter than server (TR6-17) |
| import | POST /events/{id}/import (web.py:809) | filename, total, by_type, warnings, features | filename, total, by_type, warnings | features unread (TR6-12) |
| staged | GET /events/{id}/staged | features[]: import_feature row + geojson parsed | id, name, folder, geom_type, geojson.coordinates, length_m, suggestion | OK |
| assign | POST /events/{id}/assign | course: course_id, distance_m, warnings; poi: poi_ids; discard: discarded | warnings, distance_m | OK |
| courses | GET /events/{id}/courses (web.py:859) | courses[]: course row (geojson as STRING); pois[]: poi row + layer_name, layer_icon, layer_color, distance_along_m, course_name, label_text, label_auto, show_labels, course_ids | courses: id, name, distance_m, color, bib_color, bib_color_name, geojson (JSON.parse); pois: id, name, distance_along_m, course_name, layer_color, layer_icon, poi_type, course_ids, show_labels, label, label_auto, lat, lon, what3words | OK; export reads a DOM span that no longer exists (TR6-3) |
| course update / reorder / delete | POST | course row / {ordered} / {deleted} | none | per-row save reloads (TR6-15) |
| poi add / update / reorder / move / delete | POST | poi row / poi row / {ordered} / {moved} / {deleted} | moved | OK |
| categories | GET /events/{id}/categories (web.py:1096) | poi_categories[]: row + place_count; roster_roles[]: row + in_use; lead_divisions[]: row + in_use | key, name, color, icon, staffed, visible, show_labels, place_count; key, name, in_use; key, name, in_use | OK |
| category/role/leader add, rename, reorder, delete | POST | row / row / {ordered} / {deleted} | created.name (layer add) | OK |
| tracking | GET+POST /events/{id}/tracking (web.py:1013-1035) | area_mi, enabled, running, callsign, has_callsign, callsign_problem, tracked, filter, error | all nine | OK |
| roster | GET /events/{id}/roster (web.py:977) | roster[]: roster row + poi_name; categories[{key,name}]; pois[] (staffed only, full list_pois shape); ignored[] | roster: station_key, bound_key, display_label, operator_name, category, expects_aprs, poi_name, poi_id; categories; pois: id, name | ignored unread (TR6-12); `S.pois` overwritten with the staffed subset |
| roster save / delete | POST | roster row / {ok} | none | OK |
| links | GET+POST /events/{id}/links (web.py:1338-1398) | slug; links[]: access_token row (id, event_id, token, role, label, revoked, created_at, last_used) + role_label | slug, id, token, role, role_label, label, revoked, last_used | last_used raw ISO (TR6-14) |
| organizations | GET /api/setup/organizations | organizations[]: row + event_count, admin_count | id, name, slug, contact, event_count, admin_count | OK |
| org create/update/delete | POST | row / row / {deleted} | name | OK |
| users | GET /api/setup/users (web.py:1453) | users[]: as_dict + is_active, last_login, events[]; roles[{value,label}]; organizations[] | username, display_name, role_label, is_system_admin, is_org_admin, events.length, is_active, id; roles; organizations | OK |
| user create/update/delete | POST | as_dict (no is_active/events) / as_dict / {deleted} | none (list reloaded) | OK |
| report page | GET /setup/events/{id}/report (HTML) | `<script id="courses">` [{name,color,coordinates}], `.mini[data-lat][data-lon]` | coordinates, color, data-lat, data-lon | OK |
