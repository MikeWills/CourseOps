# Live map client -> web.py boundary audit (trace layer 1)

## Summary
Scope: every interactive control in `src/courseops/static/index.html` and everything `app.js`/`icons.js` wires (static ids, rendered rows, map popups, layer switches, panel/sheet controls, keyboard and drag handlers), followed to each `fetch()`/WebSocket use and matched against the live-map routes in `src/courseops/web.py` (`/e/{slug}/{token}`, `/api/{slug}/{token}/...`, `/ws/{slug}/{token}`, manifest, `/help/`). Files read in full: index.html (207 lines), app.js (2787), icons.js (75), web.py lines 60-475 and 1550-2110; supporting reads of `db.py` (tracking/bind helpers), `incidents.py`, `leaders.py`, `hub.py`, `access.py`.
Every client request reaches a declared route with the right method, path shape and body keys; no typo'd selector, no undefined handler, no fetch to a nonexistent URL.
Findings: 0 Critical, 2 High, 3 Medium, 3 Low. Three routes in the group have no client caller (tests only).

## Findings

### TR1-1: `station_status` socket message is keyed by the roster key, but the client roster map is keyed by the tracking key, so other screens never see a matched station's status change
- Severity: High
- File: src/courseops/web.py:1727-1737; src/courseops/static/app.js:2472, 2545-2554
- What: `build_state` tells the client to key the roster by `tracking_key` (the bound SSID, e.g. `WX0MIK-5`) and app.js does (`state.roster = new Map(data.roster.map((r) => [r.tracking_key || r.station_key, r]))`). The `station_status` broadcast carries `row["station_key"]` (the roster's own key, e.g. `WX0MIK`). The socket handler does `state.roster.get(message.station_key)`; for any entry that has been matched via Adopt (`db.change_station_key` binds in both the bare-callsign and different-callsign cases, never renames) that lookup misses and the message is dropped.
- Evidence: web.py:1729 `"station_key": row["station_key"],` / app.js:2546 `const entry = state.roster.get(message.station_key); if (entry) { ... renderStations(); }`
- Why it matters: NCS marks a matched sweep "Rolling"; their own tab shows it (optimistic update at app.js:1141), but every other browser - a second NCS operator, Logistics waiting for the sweep to say the road is clear - keeps the old status until an unrelated resync happens. A silent, one-sided failure on the exact data Logistics acts on.
- Fix: publish `db.tracking_key(row)` in the payload (e.g. `"tracking_key"`) and have the client look up `message.tracking_key || message.station_key`; or resolve the map key on the client by scanning for `entry.station_key === message.station_key`. Add a test with a bound entry asserting the socket message carries the tracking key.
- Effort: S

### TR1-2: A failed `/state` fetch during reconnect kills the reconnect loop for good
- Severity: High
- File: src/courseops/static/app.js:2377-2387, 2523-2525, 2576-2586
- What: `loadState()` has no try/catch around `fetch`. `scheduleReconnect` does `await loadState(); connect();` inside a `setTimeout(async ...)`; if the fetch rejects (phone still in a dead zone when the timer fires, which is the normal case), the rejection is unhandled, `connect()` never runs and nothing schedules another attempt. The `resync` message path (`loadState();` unawaited at 2524) has the same unhandled rejection, though that one only loses one refresh.
- Evidence: app.js:2578-2584 `setTimeout(async () => { setConnection('connecting', 'Connecting…'); await loadState(); connect(); }, delay);` and app.js:2378 `const response = await fetch(...)` with no guard; no `unhandledrejection` handler exists anywhere in the client.
- Why it matters: The design rule in the file header ("a phone back from a dead zone gets a correct picture") depends on this loop. In practice a socket drops, the 1-2 s retry fires while still offline, and from then on the badge reads "Connecting…" until the operator reloads the page by hand. On iOS the OS may reload the tab and mask it; on a desktop NCS screen it will not.
- Fix: wrap the fetch in `loadState` in try/catch returning `false` (and set the badge), and in `scheduleReconnect` always fall through to `connect()` (or re-call `scheduleReconnect()` on failure) so the backoff continues.
- Effort: S

### TR1-3: After dropping a pin the "focus the bib/note field" selector matches nothing
- Severity: Medium
- File: src/courseops/static/app.js:1918-1923 vs 2181, 2190, 2290
- What: `createIncident` looks for `[data-bib-for="<id>"]` / `[data-note-for="<id>"]`. Those attributes were replaced by `data-edit-key="bib:<id>"` / `"note:<id>"` in commit 6eeedb8 (#93), whose message calls the old attributes "unused" - but this querySelector still reads them. `field` is always null, so the focus/select never happens.
- Evidence: app.js:1920 `` ? `[data-note-for="${created.id}"]` : `[data-bib-for="${created.id}"]` `` ; grep for `bib-for`/`note-for` finds no other occurrence in static/.
- Why it matters: The comment above it promises "straight into the field that will be filled in next". It is a convenience, not a requirement (the guides only say "type the bib on the row that appears"), but it is dead code that looks like a feature, and on a phone the operator has to find and tap the field while holding a microphone.
- Fix: query `[data-edit-key="bib:${created.id}"]` / `[data-edit-key="note:${created.id}"]` (use `CSS.escape` as `restoreFieldEdit` does), or delete the block and the comment.
- Effort: S

### TR1-4: Layer and role switches are rendered once; a resync after setup adds or renames a layer leaves them stale
- Severity: Medium
- File: src/courseops/static/app.js:2488 (`if (firstLoad) renderLayerToggles();`), 1028-1075
- What: Every `POST /api/setup/events/{id}/...` publishes `resync` (web.py:446-456) precisely so setup changes reach the field. `applyState` on resync updates `state.poiCategories` and `state.roleLabels` and redraws the POIs, but `renderLayerToggles()` runs only on first load. A layer added in setup mid-event draws on the map with no switch in "Places"; a renamed layer or station role keeps its old name in the switch list; an event that started with zero layers keeps `#place-layer-section` hidden after the first one is created.
- Evidence: app.js:1046 `document.getElementById('place-layer-section').hidden = !state.poiCategories.length;` is only reachable from `renderLayerToggles`, which is only called at 2488 under `firstLoad`.
- Why it matters: A club adding a "Traffic control" layer on the morning cannot be switched off (or on, if `visible` is false) by anyone already holding the page; the workaround is a manual reload nobody knows to do.
- Fix: call `renderLayerToggles()` on every `applyState` (it reads checked state from `state.layerPrefs`, so re-rendering loses nothing), or re-render only when the category/role lists differ from the last render.
- Effort: S

### TR1-5: Leader Undo and Clear swallow a server refusal
- Severity: Medium
- File: src/courseops/static/app.js:1523-1538 (`resetLeader`), 1540-1550 (`undoSighting`)
- What: Both `await fetch(...)` without checking `response.ok`. The routes (web.py:1996-2034) return 400 on a bad `course_id`/`division` and 403 when the role lacks `leaders`; only a network-level exception reaches the catch. `recordSighting` beside them does check `response.ok`.
- Evidence: app.js:1528-1534 `await fetch(`/api/${M.slug}/${M.token}/leaders/reset`, {...});` followed directly by `} catch (err) { setLocateStatus('Could not clear'); }`.
- Why it matters: NCS confirms "Clear every First male sighting for Half? This cannot be undone", gets a 4xx, and sees nothing - the list stays as it was and reads as "the button did nothing". Today the client only shows these buttons when `can('leaders')`, so the 403 path is theoretical; the 400 path is the one a stale `division` key after a setup edit could hit.
- Fix: `if (!response.ok) throw new Error(...)` in both, matching `recordSighting`.
- Effort: S

### TR1-6: `changed_by` is truncated to 12 characters for station status but 24 for incidents and sightings
- Severity: Low
- File: src/courseops/web.py:1717; src/courseops/incidents.py:69 (`MAX_WHO_LENGTH = 24`); src/courseops/static/index.html:172 (`maxlength="24"`)
- What: The one operator field feeds three logs with two different caps. `"Christopher Wainwright"` is stamped whole on a pickup and as `"Christopher "` on a station status change.
- Evidence: web.py:1717 `changed_by = (body.get("changed_by") or "").strip()[:12] or None`
- Why it matters: The same shift's entries in `roster_status_log` and `incident_log` do not match each other on a handover read. Cosmetic, but the rule "the operator callsign is a log annotation" implies one shape.
- Fix: use one constant (24) in both places.
- Effort: S

### TR1-7: `#sheet-grip` is `role="button" tabindex="0"` but has no keyboard handler
- Severity: Low
- File: src/courseops/static/index.html:75; src/courseops/static/app.js:2764
- What: Only a `click` listener is attached. Enter/Space on the focused grip do nothing, unlike the fold headings (app.js:402-404) which handle both.
- Evidence: app.js:2764 `document.getElementById('sheet-grip').addEventListener('click', () => setSheet(false));`
- Why it matters: A focusable element announced as a button that ignores the keyboard. Minor: `#sheet-collapse` beside it is a real button and does the same job.
- Fix: add the same keydown handler as `makeFoldable`, or drop `role`/`tabindex` and leave it decorative.
- Effort: S

### TR1-8: Payload and state carried but never read
- Severity: Low
- File: src/courseops/web.py:375 (`pickups_waiting`), 1623/1625 (`payload["role"]` assigned twice); src/courseops/static/app.js:31-34 + 2401 (`incidentKinds`), 80 + 2402 (`divisions`), 1707 (`.filter((poi) => !leader.course_id || true)`)
- What: `pickups_waiting` is sent in every snapshot and never read (the client computes the count from the list at app.js:2108). `state.incidentKinds` and `state.divisions` are populated and never used (the pin-kind buttons are static HTML; divisions arrive inside `leaders`). The picker filter is a no-op. `role` is set twice.
- Evidence: grep for `pickups_waiting`, `incidentKinds`, `state.divisions` in app.js shows assignment only.
- Why it matters: Cleanliness only; each is a place a future reader will assume something depends on.
- Fix: delete, or (for `pickups_waiting`) use it for the badge so the server's definition of "waiting" is the one shown.
- Effort: S

## Unconfirmed
none

## Clean
- Every client fetch URL has a declared route with the matching method: `/state` GET, `/station/{key}/status`, `/incidents`, `/incidents/{id}`, `/incidents/{id}/status`, `/incidents/{id}/delete`, `/ssid/{adopt,unbind,ignore,unignore}`, `/leaders/{sighting,undo,reset}` all POST; `/ws/{slug}/{token}`; `/api/{slug}/{token}/manifest.webmanifest`; `/help/{page}`.
- Route ordering: setup routes (web.py:617-1530) are declared before the `/api/{slug}/{token}/...` family and the literal `/incidents/{id}/status` and `/delete` routes precede `/incidents/{incident_id}`; no setup path can be captured by a live route (segment counts differ).
- Body keys and types match on every write: `op_status`/`changed_by`; `lat`/`lon` (numbers, `float()` server side), `kind`, `changed_by`; `status`; `bib`/`note` (null clears, server whitelist `{bib, note, assigned_to, lat, lon}`); `from_station_key`/`to_station_key`; `station_key` (+`reason`); `course_id`/`division`/`poi_id`/`bib`/`changed_by` (ints coerced with `int()`). Snake case throughout; no lat/latitude drift.
- Path param `station_key` is `encodeURIComponent`-ed and the server resolves either the roster key or the bound key via `db.resolve_station_key` (db.py:244), so the tracking-keyed client key is accepted on writes.
- Response shapes consumed: `/state` keys read by `applyState` all exist in `build_state` + the route additions (`role`, `role_label`, `can_write`, `capabilities`, `nearby`, `ignored`); `POST /incidents` returns `Incident.as_dict()` with `id` and `kind`; other responses are ignored and the client relies on the broadcast, which the server sends before responding.
- Socket messages: `position` (hub.py:86-111), `station_status`, `incident` (+`change`), `leaders`, `nearby`, `resync` - every key the client reads is present; `nearby` lacks `roster_candidates` and the client defaults it to `[]`. Capability-gated messages (`nearby`, `incident`) are gated on the server; client renders `ssid`/`incident_report` sections from `capabilities` only.
- Capability names used by `can()` in app.js (`ssid`, `stations`, `leaders`, `incidents`, `incident_report`) match `access.py:68-73` exactly.
- Error visibility: every write except TR1-5 checks `response.ok` and surfaces failure via `setLocateStatus`; station and incident status changes roll back the optimistic update. `resolveSsid` shows the server's `detail`. `loadState` 404 shows "Access denied" (except the network-failure case in TR1-2).
- All static ids referenced by `getElementById`/`querySelector` in app.js exist in index.html (conn, conn-text, event-name, role-badge, help-link, locate-btn, locate-status, sheet, sheet-toggle, sheet-toggle-label, sheet-badge, sheet-grip, sheet-header, sheet-brand, sheet-collapse, sheet-reopen, stations-panel, stations-collapse, stations-reopen, side-panel-title, ssid-section, ssid-alerts, ignored-section, ignored-list, ignored-count, leader-list, incident-section, incident-count, pin-kind, pickup-sort, pin-actions, incident-add, incident-here, incident-hint, incident-list, note-section, note-list, note-count, station-list, station-count, course-list, layer-list, place-layer-section, place-layer-list, operator-box, operator-initials, role-note, map). All handlers attach after the DOM (scripts at the end of body).
- `glyphSvg`/`glyphLabel` (icons.js) are defined before app.js loads and every glyph name used falls back to `pin`.
- Manifest: `__MANIFEST_URL__` is substituted in `map_page`; `start_url`/`scope` point at the role link; icons exist in `static/`.
- Leaflet is loaded from unpkg with SRI; no CSP header is set by the app, so nothing blocks it.
- Routes in this group with NO client caller: `GET /api/{slug}/{token}/station-log` (web.py:1918), `GET /api/{slug}/{token}/incidents/{id}/log` (1937), `POST /api/{slug}/{token}/course/{id}/bib-color` (2036). Exercised by tests only; bib colour is set from setup via `/api/setup/events/{id}/courses/{course_id}` (setup.js:1067). Not defects, but they are surface area nobody reaches from a screen.

## Appendix: control inventory

| # | control | handler | request | route | status |
|---|---|---|---|---|---|
| 1 | page `/e/{slug}/{token}` | - | GET | web.py:1560 `map_page` | OK |
| 2 | `<link rel=manifest>` (index.html:20) | `__MANIFEST_URL__` substitution web.py:1568 | GET `/api/{slug}/{token}/manifest.webmanifest` | web.py:1571 | OK |
| 3 | boot IIFE (app.js:2785) | `loadState` app.js:2377 | GET `/api/{slug}/{token}/state` | web.py:1616 | OK (network failure unhandled: TR1-2) |
| 4 | `connect()` app.js:2506 | socket open/message/close/error | WS `/ws/{slug}/{token}` | web.py:2061 | OK |
| 5 | WS `resync` (app.js:2523) | `loadState()` unawaited | GET `/state` | web.py:1616 | OK (rejection unhandled: TR1-2) |
| 6 | WS `station_status` (app.js:2545) | in-place roster update | - | publisher web.py:1737 | MISMATCH (TR1-1) |
| 7 | WS `position` / `leaders` / `incident` / `nearby` | app.js:2527-2565 | - | hub.py:86, web.py:1749, 1957, 187 | OK |
| 8 | `#help-link` (index.html:50) | href set app.js:2461 | GET `/help/<role page>` | web.py:2096 | OK |
| 9 | `#locate-btn` click | app.js:2682 | none (geolocation, local) | - | OK |
| 10 | `#sheet-toggle` click | app.js:2763 `setSheet` | none | - | OK |
| 11 | `#sheet-grip` click | app.js:2764 | none | - | OK (no keyboard: TR1-7) |
| 12 | `#sheet-collapse` click | app.js:571 | none | - | OK |
| 13 | `#sheet-reopen` click | app.js:575 | none | - | OK |
| 14 | `#stations-collapse` click | app.js:579 | none | - | OK |
| 15 | `#stations-reopen` click | app.js:577 | none | - | OK |
| 16 | section `h2` fold click/Enter/Space (all `.sheet-section`) | app.js:401-404 | none (prefs) | - | OK |
| 17 | `.pin-kind-btn` x2 click | app.js:2341 | none | - | OK |
| 18 | `.sort-btn` x2 click | app.js:2328 | none | - | OK |
| 19 | `#incident-add` click | app.js:2358 `setDroppingPin` | none | - | OK |
| 20 | map click while dropping | app.js:2363 -> `createIncident` 1901 | POST `/incidents` {lat:num, lon:num, kind:str, changed_by:str} | web.py:1767 | OK (focus selector dead: TR1-3) |
| 21 | `#incident-here` click | app.js:2356 `dropPinHere` 1980 -> `createIncident` | POST `/incidents` (same body) | web.py:1767 | OK |
| 22 | `#operator-initials` input | app.js:2722 | none (localStorage) | - | OK |
| 23 | map `dragstart` / `zoomend` | app.js:335, 603 | none | - | OK |
| 24 | `WIDE`/`SIDEBAR` media change | app.js:582-583 | none | - | OK |
| 25 | `pageshow` / `visibilitychange` | app.js:2778-2781 | none | - | OK |
| 26 | course row checkbox change | app.js:945 | none | - | OK |
| 27 | course row "Top" button | app.js:957 `setCourseStack` | none | - | OK |
| 28 | course row grip mousedown / ArrowUp/Down keydown | app.js:962-975 | none | - | OK |
| 29 | course list dragstart/dragover/dragend | app.js:990-1010 | none | - | OK |
| 30 | "Back to the club's order" button | app.js:984 | none | - | OK |
| 31 | People layer toggles (7) change | app.js:1036 | none | - | OK |
| 32 | "Place names (zoomed in)" toggle | app.js:1055 | none | - | OK |
| 33 | Place layer toggles (per category) | app.js:1067 | none | - | OK (stale after resync: TR1-4) |
| 34 | station row `.station-main` click | app.js:1238 `showOnMap` | none | - | OK |
| 35 | station row "Unmatch X" | app.js:1255 `resolveSsid('unbind')` | POST `/ssid/unbind` {station_key:str} | web.py:1866 | OK |
| 36 | station row op-status buttons (pending/active/closed) | app.js:1271 `setStationStatus` 1134 | POST `/station/{key}/status` {op_status:str, changed_by:str} | web.py:1703 | OK (broadcast key: TR1-1) |
| 37 | SSID alert "This is <candidate>" | app.js:1393 `resolveSsid('adopt')` | POST `/ssid/adopt` {from_station_key, to_station_key} | web.py:1844 | OK |
| 38 | SSID alert "This is…" select change | app.js:1415 | POST `/ssid/adopt` (same) | web.py:1844 | OK |
| 39 | SSID alert "Ignore" | app.js:1429 | POST `/ssid/ignore` {station_key, reason} | web.py:1886 | OK |
| 40 | Ignored row "Unignore" | app.js:1468 | POST `/ssid/unignore` {station_key} | web.py:1902 | OK |
| 41 | leader bib input (`data-edit-key`) | read by 42/43; no own handler | none | - | OK |
| 42 | leader "Passed <next>" | app.js:1696 `recordSighting` 1504 | POST `/leaders/sighting` {course_id:int, division:str, poi_id:int, bib:str|null, changed_by} | web.py:1969 | OK |
| 43 | leader "At…" select change | app.js:1711 `recordSighting` | POST `/leaders/sighting` (poi_id Number()) | web.py:1969 | OK |
| 44 | leader "Undo" | app.js:1725 `undoSighting` 1540 | POST `/leaders/undo` {course_id, division} | web.py:1996 | OK (4xx silent: TR1-5) |
| 45 | leader "Clear" (confirm) | app.js:1734 `resetLeader` 1523 | POST `/leaders/reset` {course_id, division} | web.py:2014 | OK (4xx silent: TR1-5) |
| 46 | pickup row `.incident-main` click | app.js:2161 `showOnMap` | none | - | OK |
| 47 | pickup bib input change / Enter | app.js:2198, 2200 -> `editIncident` 2003 | POST `/incidents/{id}` {bib:str|null, changed_by} | web.py:1824 | OK |
| 48 | pickup note input change / Enter | app.js:2199 -> `editIncident` | POST `/incidents/{id}` {note:str|null, changed_by} | web.py:1824 | OK |
| 49 | pickup delete (x) (confirm) | app.js:2068 `deleteIncident` 2036 | POST `/incidents/{id}/delete` {} | web.py:1807 | OK |
| 50 | pickup status buttons (server `incident_statuses`, 5) | app.js:2227 `setIncidentStatus` 1871 | POST `/incidents/{id}/status` {status:str, changed_by} | web.py:1788 | OK |
| 51 | note row `.incident-main` click | app.js:2279 `showOnMap` | none | - | OK |
| 52 | note text input change / Enter | app.js:2291, 2297 -> `editIncident` | POST `/incidents/{id}` {note, changed_by} | web.py:1824 | OK |
| 53 | note delete (x) (confirm) | app.js:2304 `deleteButton` | POST `/incidents/{id}/delete` {} | web.py:1807 | OK |
| 54 | station marker popup | `bindPopup` app.js:689 | none | - | OK |
| 55 | POI marker popup / permanent name tooltip | app.js:835, 851 | none | - | OK |
| 56 | course line popup | app.js:771 | none | - | OK |
| 57 | incident marker popup | app.js:1856 | none | - | OK |
| 58 | "You are here" marker popup | app.js:2658 | none | - | OK |
| 59 | 15 s age redraw timer | app.js:2590 | none | - | OK |
| 60 | (no control) | - | GET `/api/{slug}/{token}/station-log` | web.py:1918 | DEAD from the client (tests only) |
| 61 | (no control) | - | GET `/api/{slug}/{token}/incidents/{id}/log` | web.py:1937 | DEAD from the client (tests only) |
| 62 | (no control) | - | POST `/api/{slug}/{token}/course/{id}/bib-color` | web.py:2036 | DEAD from the client (setup uses `/api/setup/events/{id}/courses/{cid}`) |
