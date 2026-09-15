# Setup client traceability audit (layer 2 of 5)

## Summary
Scope: every interactive control in `src/courseops/static/setup.html` and every element `setup.js` builds with a handler, followed to the `api()`/`post()`/raw `fetch` call and to the matching route in `src/courseops/web.py` (lines 589-1548, `/setup`, `/setup/events/{id}/report`, `/api/setup/...`). Route bodies were opened only far enough to see which request keys they consume and what they return (`admin.py`, `users.py`, `categories.py`, `access.py`). `report.py` was checked for outbound requests (none beyond tiles/Leaflet). `icons.js` exports (`POI_GLYPHS`, `glyphSvg`, `glyphLabel`) confirmed.
Files read: setup.html (554 lines), setup.js (2484), icons.js, web.py 440-1560 and 1678-1688, report.py 260-315, admin.py, users.py, categories.py, access.py, importer.py (selected), build.py.
Counts: 0 Critical, 2 High, 6 Medium, 4 Low. Every URL the client builds has a route; every literal route is declared before its parameterised sibling; every `bindSaveAll` table sends a diff and its route accepts a partial payload. The appendix has one row per control (85 control rows plus 3 dead-route rows).

## Findings

### TR2-1: Export CSV writes an empty Coordinates column for every place
- Severity: High
- File: src/courseops/static/setup.js:2114 (reader), src/courseops/static/setup.js:1130-1140 (cell it reads)
- What: The exporter reads `tr.querySelector('.coords span')`, but since the Places map picker (#111, commit 3a233d3) the `.coords` cell contains two `<input>`s (`data-plat`, `data-plon`) and a copy button - no `<span>`. The selector matches nothing and the fallback `''` is written.
- Evidence: `(tr.querySelector('.coords span') || {}).textContent || ''` at :2114; the cell at :1130 is `<td class="coords"><input ... data-plat=...><input ... data-plon=...>${iconBtn('copy', ...)}</td>`.
- Why it matters: The CHANGELOG entry for Export CSV says the column is "layer, name, coordinates and What3Words ... what gets shared with the organizer and read from on air". The file goes out with the coordinates blank and nothing on screen says so; `banner('Exported N place(s).')` reports success.
- Fix: Read the two inputs: `` `${tr.querySelector('[data-plat]').value}, ${tr.querySelector('[data-plon]').value}` `` (guarded for a row with no coordinates), which also keeps the "unsaved edits included" promise.
- Effort: S

### TR2-2: A wrong password shows "Sign in again." instead of the server's message
- Severity: High
- File: src/courseops/static/setup.js:51 (interceptor), src/courseops/static/setup.js:120 (login call), src/courseops/web.py:694-696 (route)
- What: `api()` treats every 401 as "session expired": it calls `showGate(false)` and throws a fixed `'Sign in again.'` before reading the body. The login route itself answers 401 with `detail="Incorrect username or password."` (users.py:406, also used for a disabled account), so that text never reaches the form; `showGate(false)` also wipes any notice that was on the gate (e.g. "Account created. Sign in with it to continue." after first run).
- Evidence: `if (response.status === 401) { showGate(false); throw new Error('Sign in again.'); }` (:51); `const data = await post('/api/setup/login', body);` (:120); `raise HTTPException(status_code=401, detail=str(exc))` (:696).
- Why it matters: Every mistyped password on race morning gets a message that reads like a session problem, and a deactivated administrator is told to "sign in again" indefinitely with no hint that the account is disabled. The first-run flow's "Account created" notice disappears the moment the new password is mistyped once.
- Fix: Skip the 401 interception when `path === '/api/setup/login'` (or when `S.user === null` and the gate is already showing), and let the route's `detail` surface as it does for every other error.
- Effort: S

### TR2-3: "New version - reload" appears falsely after signing in from a signed-out page
- Severity: Medium
- File: src/courseops/static/setup.js:1609-1629, src/courseops/static/setup.js:2474-2480, src/courseops/web.py:634
- What: `S.loadedVersion` is recorded once from the startup `/api/setup/session` call. Signed out, the route sends `build: ""` (web.py:634, deliberately) so the key falls back to the version string. After sign-in nothing re-records the key; the next `pollVersion()` (visibility change, or 5 min) sees a non-empty `build` and `versionKey` now differs from `loadedVersion`, so the notice shows although the page IS the current code.
- Evidence: `"build": build.build_id() if user else ""` (web.py:634); `if (S.loadedVersion === undefined) S.loadedVersion = versionKey(info);` (:1616), called only from the startup IIFE (:2478); `el.hidden = now === S.loadedVersion;` (:1628). `start()` (:2458) does not touch `S.loadedVersion`.
- Why it matters: On the deployed server `build_id()` is `git describe` output, so this fires on every fresh sign-in (tab-switch away and back is enough). A notice that cries wolf on the one screen it lives on trains the officer to ignore it on the day it is true.
- Fix: After a successful login (setup.js:121) re-fetch `/api/setup/session` and reset `S.loadedVersion = versionKey(data)` (or have `checkVersion` ignore a comparison where the stored key was recorded without a build).
- Effort: S

### TR2-4: Course table keeps a per-row Save that reloads the whole table
- Severity: Medium
- File: src/courseops/static/setup.js:1044, 1060-1073
- What: Courses is the one editable setup table still on the pattern CLAUDE.md forbids ("Never reload a table that holds unsaved edits ... `bindSaveAll` is the one implementation; wire a new editable table to it"). Each row has `data-savec`; its handler posts all four fields whether changed or not and then calls `loadCourses()`, which re-renders both the courses table AND the Places table below it.
- Evidence: `iconBtn('save', {'data-savec': c.id}, ...)` (:1044); handler posts `{name, color, bib_color, bib_color_name}` unconditionally then `loadCourses();` (:1064-1071).
- Why it matters: Editing two course names (or a name and a bib colour) and pressing Save on one discards the other silently - the exact failure that cost a real user twelve renames on the Places table. The re-render also discards any unsaved Places-table edits sitting in the same `loadCourses()` output when the Courses tab is used from a shared session.
- Fix: Replace the per-row button with `bindSaveAll({table:'course-table', fields:[name,color,bib_color,bib_color_name] ...})`; the route (`admin.update_course`, web.py:883) already accepts a partial payload.
- Effort: S

### TR2-5: Six mutating handlers swallow errors - the screen does nothing
- Severity: Medium
- File: src/courseops/static/setup.js:1074-1079 (delete course), :1406-1411 (delete place), :2191-2197 (remove roster entry), :2320-2326 (issue another link), :2331-2337 (save link label), :2339-2348 (revoke link), :2350-2358 (reissue role)
- What: These `async` click/change handlers `await post(...)` with no `try/catch`. `api()` throws on any non-2xx (:60), so a 400/403/409 becomes an unhandled promise rejection: no banner, no reload, the row stays exactly as it was. (401 is handled inside `api()` and is fine.)
- Evidence: e.g. `await post(\`/api/setup/events/${S.eventId}/courses/${b.dataset.delc}/delete\`); loadCourses();` (:1077-1078) with no catch; compare the guarded siblings at :446-450 and :1727-1731.
- Why it matters: `/links` returns 400 for an unknown role or action and 403 "Not your event." after a club reassignment; roster delete of an entry NCS has since rebound goes the same way. The person presses Revoke on a leaked link, sees nothing change, and cannot tell "already done" from "refused". A label edit that fails is never reported and is lost on the next reload.
- Fix: Wrap each in the `try { ... } catch (err) { banner(err.message, true); }` the other handlers use.
- Effort: S

### TR2-6: `S.poiCategories` is cached across events - the wrong club's layers after "Work on this"
- Severity: Medium
- File: src/courseops/static/setup.js:590-603 (selectEvent), :805-813 (fillAssignTypes), :1017-1020 (loadCourses)
- What: The layer list is fetched lazily (`if (!S.poiCategories)`) and only invalidated by a layer reorder (:1772) or a visit to Layers/Roles/Leaders (:1673). `selectEvent()` clears the provisional pin and nothing else, so a host with two events keeps event A's layers when working on event B: the Import tab's Type list, the Places per-row Layer dropdown, the bulk Move target, the Add-place layer list and the picker's marker colours are all built from A.
- Evidence: `selectEvent` sets `S.eventId`, calls `gateOnEvent` and `loadEvents()` only (:597-602); `S.poiCategories` assigned at :808, :1018, :1673 and nulled only at :1772.
- Why it matters: With the default seven layers the keys coincide and nothing is visible; with a club-added layer ("Medical", key `medical`) on A but not B, adding a place on B offers "Medical", the server refuses with "No such layer" (admin.py:303 -> `categories.get_poi_category`), and a place already in B's own custom layer renders with the first option selected - which `bindSaveAll` records as the original, so switching away and back is not flagged as a change. The Import review's "looks like X" names also come from the wrong event.
- Fix: `S.poiCategories = null;` in `selectEvent()` (and in the delete-event handler at :573).
- Effort: S

### TR2-7: Every re-render stacks another set of listeners on the table containers
- Severity: Medium
- File: src/courseops/static/setup.js:388-407 (bindReorder), :1486-1487 (bindSaveAll)
- What: `bindReorder` adds `dragstart/dragover/dragend` and `bindSaveAll` adds `input/change` to the CONTAINER (`#poi-table`, `#course-table`, `#layer-table`, `#leader-table`), whose `innerHTML` is replaced on every load but which itself persists. Nothing removes the previous listeners, so after k loads a drag runs k `dragend` closures, each calling its own `persist()`.
- Evidence: `tableEl.addEventListener('dragend', () => { ... save(); });` (:401-407) on `$('poi-table')` passed at :1342, called from `loadCourses()` which runs on every save, delete, add, reorder and tab visit; `root.addEventListener('input', refresh);` (:1486) likewise.
- Why it matters: After a Places session of twenty saves, one drag issues twenty `POST .../pois/reorder` requests and twenty `resync` broadcasts to every field phone (web.py:445-456 publishes once per successful POST). Each keystroke in a 78-row table also runs k full-table diffs. It is invisible until the tab has been used for a while, which is race week.
- Fix: Keep one bound flag per container (e.g. `root.dataset.bound`) and register the container-level listeners once, reading `persist`/`fields` from a property the caller updates; or replace the container with a fresh `<table>` element each render and bind to that.
- Effort: M

### TR2-8: The setup UI cannot set an event's centre, so the picker's documented fallback is unreachable
- Severity: Medium
- File: src/courseops/static/setup.js:1934-1938 (fallback), :697-704 (create), :687-691 (update); src/courseops/web.py:758-787; src/courseops/importer.py:384 (`suggest_event_center`, never called)
- What: `placeMapView` falls back to `event.center_lat/center_lon` before the country view - the case CLAUDE.md names for #108 (a parade with nothing to import). But the event form has no position fields, the client never sends `center_lat`/`center_lon`/`zoom` (the routes accept them, admin.py:95-96, :110-113), and `importer.suggest_event_center` has no caller. Only the CLI `add-event --lat --lon` sets a centre.
- Evidence: `if (event && event.center_lat != null && event.center_lon != null) { map.setView(...) }` (:1935); create body at :697-704 has no centre keys; `grep suggest_event_center` finds only the definition.
- Why it matters: For exactly the event the picker exists for - no file, no places yet - the map opens on the whole United States at zoom 4 and the first click lands somewhere in Kansas unless the officer zooms in by hand first. The live map (app.js:899) has the same fallback and the same gap.
- Fix: Either add lat/lon (or a "centre the map here" control) to the event form and send `center_lat`/`center_lon`, or call `suggest_event_center` after an import and on `create_poi` when the event has no centre yet.
- Effort: M

### TR2-9: Import upload turns a non-JSON error page into "Unexpected token <"
- Severity: Low
- File: src/courseops/static/setup.js:787-790
- What: The upload uses raw `fetch` and `await response.json()` before checking `response.ok`. A 413 from Apache (`LimitRequestBody`) or a proxy 502 is an HTML body, so the banner shows a JSON parse error instead of "file too large".
- Evidence: `const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Import failed');`
- Why it matters: The KMZ that is too big for the proxy is the one the organizer sends; the person reading "Unexpected token '<'" has no idea the size is the problem.
- Fix: `.json().catch(() => ({}))` as `api()` already does at :59, then throw `data.detail || \`Upload failed (${response.status})\``.
- Effort: S

### TR2-10: Picking a point in review forces `assign-type` to a layer that may not exist
- Severity: Low
- File: src/courseops/static/setup.js:976
- What: `togglePick` sets `$('assign-type').value = 'aid_station'` when any point is picked. The options are `course` plus the event's layer keys (:810-812); a club that deleted the default Aid station layer has no such option, so the select goes to `selectedIndex -1` (value `''`), and Assign posts `poi_type: ''`, which the server defaults back to `'aid_station'` (admin.py:167) and then refuses.
- Evidence: `$('assign-type').value = allLines ? 'course' : 'aid_station';`
- Why it matters: The layer taxonomy is the club's (CLAUDE.md); this is one of the hardcoded places the rule says should not exist. The select shows blank and Assign fails with a message about a layer the club removed on purpose.
- Fix: Choose the first staffed layer from `S.poiCategories`, falling back to the first layer; set `'course'` only when all picked are lines.
- Effort: S

### TR2-11: Cancel on an edited event leaves an event admin with a "New event" form they cannot submit
- Severity: Low
- File: src/courseops/static/setup.js:650-678, :522
- What: `editEvent` un-hides `#event-form` for anyone who may edit (correct - `require_event_admin` allows it). `resetEventForm` retitles it "New event" but does not re-hide it; only `loadEvents` (:522) restores `hidden = !may_create_events`. So Cancel (which does not call `loadEvents`) leaves an event admin looking at a create form whose submit returns 403 "You cannot create events.".
- Evidence: `$('event-cancel').addEventListener('click', () => resetEventForm());` (:680); `resetEventForm` never sets `$('event-form').hidden`.
- Fix: `$('event-form').hidden = !S.user.may_create_events;` at the end of `resetEventForm`.
- Effort: S

### TR2-12: Two server capabilities have no control in the client
- Severity: Low
- File: src/courseops/web.py:713 (`POST /api/setup/password`), src/courseops/web.py:1521-1523 (`event_ids` on `POST /api/setup/users/{id}`)
- What: Change-own-password (re-authenticating with `current_password`) is implemented and tested on the server but nothing in setup.html/setup.js calls it; the only password UI is the manager's "Set a password" per-row button, which uses `/users/{id}` with `{password}`. Likewise `event_ids` on user update is consumed by the route but no control edits an existing administrator's event list; events can only be chosen at creation (:2444).
- Evidence: `grep "setup/password" src/courseops/static/` returns nothing; the only `event_ids` sender is the create form at :2444.
- Why it matters: An administrator cannot change their own password from the app; they must ask a manager, who then knows it. An event admin's assignment cannot be changed after creation without delete-and-recreate.
- Fix: Either add the controls (a "Change my password" on the header, an events editor on the user row) or delete the dead route/branch so the next reader does not assume the UI exists.
- Effort: M

## Unconfirmed

### TR2-U1: Pin drag on the Places picker may not work on a touch screen
- Severity: Low
- File: src/courseops/static/setup.js:1994-2006
- What: The drag is hand-rolled on Leaflet `mousedown`/`mousemove`/`mouseup` map events. Leaflet 1.9 synthesises `click` for taps on vector layers but I could not confirm from the code alone that it fires `mousedown` on a `circleMarker` for a touch start or `mousemove` on the map during a touch drag; CLAUDE.md says these tables are sorted on tablets on race morning. Needs a device check.

## Clean
- Every URL template the client builds resolves to a declared route with the same method (all POST for mutations; GET for session/events/organizations/users/staged/courses/categories/roster/tracking/links). No fetch to an undeclared path.
- Literal-before-parameterised ordering holds: `/courses/reorder` (871) before `/courses/{course_id}` (883); `/pois` (914), `/pois/reorder` (925), `/pois/move` (937) before `/pois/{poi_id}` (951); `/categories/reorder` (1155) before `/categories/{key}` (1170); `/roles/{key}/delete` (1218) before `/roles/{key}` (1235); `/leaders/reorder` (1268) before `/leaders/{key}` (1301); `/roster/delete` and `/roster` are distinct paths.
- `bindSaveAll` diff-send: Places sends only changed `name/what3words/poi_type/label/course_ids/lat/lon`; Layers only changed `name/color/staffed/visible/show_labels` (checkbox -> boolean, server `int(bool())`); Roles and Leaders only `name`. `admin.update_poi`, `categories.update_poi_category`, `rename_roster_role`, `rename_lead_division` all use `if key in payload`. `course_ids` arrives as "1,3" and `set_poi_courses` splits strings (admin.py:403-420). `lat/lon` arrive as strings and `_coordinate` parses them.
- Ids: numeric ids from `dataset` go into the URL path (parsed by FastAPI as `int`) or through `Number()`/`.map(Number)` for `course_ids`, `poi_ids`, `token_id`, `organization_id`, `event_ids`, `poi_id`; keys for layers/roles/leaders travel as strings, which is what the routes take.
- Response shapes consumed match what routes return: events (`counts.*`, `center_lat`, `slug`), organizations (`event_count`, `admin_count`), users (`role_label`, `is_active`, `events`), roster (`bound_key`, `poi_name`, `poi_id`), links (`slug`, `links[].id/role/role_label/token/label/revoked/last_used`), tracking (all nine keys), categories (`poi_categories[].place_count`, `roster_roles[].in_use`, `lead_divisions[].in_use`), assign (`distance_m`, `warnings`), import (`filename`, `total`, `by_type`, `warnings`), move (`moved`), staged (`features[].geojson` already parsed to an object; courses' `geojson` is a string and is `JSON.parse`d).
- Multipart upload: FormData field `file` matches `file: UploadFile = File(...)`.
- `gateOnEvent()` covers every `data-needs-event` panel; each event-scoped form (`poi-form`, `layer-form`, `role-form`, `leader-form`, `roster-form`) is a direct child of its panel so it is hidden with no event; `leader-form`, `role-form`, `poi-form` and `loadTracking` additionally call `needEvent()`. Elements hidden for their own reasons (`#review`, `#poi-bulk`, `#event-form`, error lines) are left alone on restore. No path found that posts to `/events/null/...`.
- No listener is attached before its element exists: every top-level `$('id')` names a static element in setup.html and the script is loaded at the end of `<body>`; every dynamic listener is bound after the `innerHTML` that creates its target. `$('poi-all')` is guarded. No handler references an undefined function; `POI_GLYPHS`, `glyphSvg`, `glyphLabel` exist in icons.js.
- Help ring: `/help/setup`, `/help/setup-people`, `/help/setup-ideas`, `/help/setup-race-week` all exist as guides and `/help/{page}` is routed.
- The report page (`report.py`) makes no API requests; its two inline scripts only format `<time>` elements and draw Leaflet mini-maps from an inline JSON block.
- Every setup `POST .../events/{id}/...` that succeeds publishes a `resync` via the middleware (web.py:445-456); no per-endpoint copies.
- `/api/setup/session` is the only endpoint polled; the poll is visibility-gated and silent on failure by design.
- `api()` handles 409-while-signed-out by recovering the session (:54-58) - checked, correct.

## Appendix: control inventory

Status: OK / MISMATCH (see finding) / DEAD / UNVERIFIED. Line numbers are setup.js unless prefixed `html:` or `web:`.

| # | Control | Handler | Request | Route (web.py) | Status |
|---|---|---|---|---|---|
| 1 | `#help-link` (html:31) | href set in `activateTab` :213 | GET `/help/{page}` | 2091-2096 | OK |
| 2 | `#version-notice` (html:44) | :142 | none (reload) | - | MISMATCH TR2-3 (false show) |
| 3 | `#logout` (html:48) | :149 | POST `/api/setup/logout` `{}` | 702 | OK (error unhandled; reload skipped on failure - trivial) |
| 4 | `#gate-form` submit, first run (html:58) | :87 | POST `/api/setup/first-user` `{username:str,password:str,display_name:str}` | 637 | OK |
| 5 | `#gate-form` submit, sign in | :87 | POST `/api/setup/login` `{username,password}` | 685 | MISMATCH TR2-2 |
| 6 | `nav .tab` x12 (html:86-97) | :217 -> `activateTab` | loaders below | - | OK |
| 7 | `[data-goto="events"]` x9 (html:157 etc.) | :220 | none | - | OK |
| 8 | `#org-form` submit, create (html:109) | :481 | POST `/api/setup/organizations` `{slug,name,contact}` | 1412 | OK |
| 9 | `#org-form` submit, edit | :481 | POST `/api/setup/organizations/{id}` `{name,contact}` | 1426 | OK |
| 10 | `#org-cancel` (html:122) | :479 | none | - | OK |
| 11 | org row `[data-edito]` | :434 -> `editOrg` | none | - | OK |
| 12 | org row `[data-delo]` | :438 | POST `/api/setup/organizations/{id}/delete` `{}` | 1440 | OK |
| 13 | `#event-form` submit, create (html:130) | :682 | POST `/api/setup/events` `{slug,name,event_date,timezone,organization_id?:int}` | 758 | OK |
| 14 | `#event-form` submit, edit | :682 | POST `/api/setup/events/{id}` `{name,event_date,timezone}` | 779 | OK |
| 15 | `#event-cancel` (html:149) | :680 | none | - | MISMATCH TR2-11 (Low) |
| 16 | `#ev-tz` select | filled :628 | none | - | OK |
| 17 | event row `[data-pick]` | :555 -> `selectEvent` | GET `/api/setup/events` (re-list) | 739 | MISMATCH TR2-6 (stale layer cache) |
| 18 | event row `a.report-link` | static href | GET `/setup/events/{id}/report` | 607 | OK |
| 19 | event row `[data-edite]` | :560 -> `editEvent` | none | - | OK |
| 20 | event row `[data-del]` | :563 | POST `/api/setup/events/{id}/delete` `{}` | 789 | OK |
| 21 | `#upload-zone` dragenter/dragover/dragleave/drop (html:163) | :727-740 -> `importFiles` | see 22 | - | OK |
| 22 | `#course-file` change (html:164) | :778 -> `uploadCourseFile` (raw fetch) | POST `/api/setup/events/{id}/import` FormData `file` | 809 | MISMATCH TR2-9 (Low) |
| 23 | review map feature click | :850 -> `togglePick` | none | - | OK |
| 24 | `#review-list .feature input` change | :869 -> `togglePick` | none | - | MISMATCH TR2-10 (Low, sets `aid_station`) |
| 25 | `#accept-suggested` (html:178) | :932 | POST `.../assign` `{kind:'poi',ids:int[],poi_type:str,name:''}` per layer | 847 | OK |
| 26 | `#pick-all` / `#pick-none` (html:179-180) | :925-926 | none | - | OK |
| 27 | `#assign-type` / `#assign-name` / `#assign-reverse` | read by 28 | - | - | OK |
| 28 | `#assign-go` (html:196) | :982 | POST `.../assign` `{kind:'course',ids,name,reverse:bool}` or `{kind:'poi',ids,poi_type,name}` | 847 | OK |
| 29 | `#assign-discard` (html:197) | :1004 | POST `.../assign` `{kind:'discard',ids}` | 847 | OK |
| 30 | course row grip drag / ArrowUp/Down | `bindReorder` :1049 | POST `.../courses/reorder` `{course_ids:int[]}` | 871 | MISMATCH TR2-7 (duplicate posts) |
| 31 | course row inputs `data-name/color/bib/bibname` | read by 32 | - | - | OK |
| 32 | course row `[data-savec]` | :1060 | POST `.../courses/{id}` `{name,color,bib_color,bib_color_name}` (all four, always) | 883 | MISMATCH TR2-4 |
| 33 | course row `[data-delc]` | :1074 | POST `.../courses/{id}/delete` `{}` | 896 | MISMATCH TR2-5 (error swallowed) |
| 34 | `#poi-filter-layer` change (html:233) | :1194 | none | - | OK |
| 35 | `#poi-filter` input (html:236) | :1193 | none | - | OK |
| 36 | `#poi-all` header checkbox | :1249 | none | - | OK |
| 37 | place row `[data-ppick]` checkbox | :1226 | none | - | OK |
| 38 | place row `[data-ppos]` focus/keydown/change | :1349-1379 -> `movePoiTo` | POST `.../pois/reorder` `{poi_ids:int[]}` | 925 | OK |
| 39 | place row grip drag / arrows | `bindReorder` :1342 | POST `.../pois/reorder` `{poi_ids:int[]}` | 925 | MISMATCH TR2-7 |
| 40 | place row `data-pname/player/plabel/w3w/plat/plon` inputs | `bindSaveAll` :1384 (input/change) | - | - | OK |
| 41 | place row `[data-prace]` checkboxes | :1269 -> writes `[data-pcourses]` hidden, dispatches input | via 42 | - | OK |
| 42 | `#poi-save-all` (html:241) | `bindSaveAll` btn.onclick :1489 | POST `.../pois/{id}` partial `{name?,what3words?,poi_type?,label?,course_ids?:"1,2",lat?:str,lon?:str}` per dirty row | 951 | OK |
| 43 | place row `[data-copyc]` | :1229 | none (clipboard) | - | OK |
| 44 | place row `[data-w3w]` input -> `[data-w3wopen]` link | :1240 | none (external what3words URL) | - | OK |
| 45 | place row `[data-delp]` | :1406 | POST `.../pois/{id}/delete` `{}` | 964 | MISMATCH TR2-5 |
| 46 | `#poi-move` (html:249) | :2083 | POST `.../pois/move` `{poi_ids:int[],poi_type:str}` | 937 | OK |
| 47 | `#poi-move-to` select (html:248) | read by 46 | - | - | MISMATCH TR2-6 (stale cache) |
| 48 | `#poi-export` (html:250) | :2106 | none (Blob download) | - | MISMATCH TR2-1 |
| 49 | `#place-map` click | :1908 | none (fills form) | - | MISMATCH TR2-8 (fallback unreachable) |
| 50 | `#place-map` pin mousedown/move/up | :1994 -> `writeRowCoordinates` | via 42 | - | UNVERIFIED on touch (TR2-U1) |
| 51 | `#place-map-wrap` `<details>` toggle (html:264) | native | none | - | OK |
| 52 | `#poi-new-lat` change/paste (html:296) | :2058-2059 `splitCoordinates` | none | - | OK |
| 53 | `#poi-form` submit (html:281) | :2061 | POST `.../pois` `{name,poi_type,lat:str,lon:str}` | 914 | OK |
| 54 | layer row grip drag / arrows | `bindReorder` :1766 | POST `.../categories/reorder` `{keys:str[]}` | 1155 | MISMATCH TR2-7 |
| 55 | layer row `data-lname/lcolor/lstaffed/lvisible/llabels` | `bindSaveAll` :1779 | - | - | OK |
| 56 | `#layer-save-all` (html:327) | btn.onclick | POST `.../categories/{key}` partial `{name?,color?,staffed?:bool,visible?:bool,show_labels?:bool}` | 1170 | OK |
| 57 | layer row `[data-ldel]` | :1796 | POST `.../categories/{key}/delete` `{}` | 1185 | OK (409 shown) |
| 58 | `#lay-icon-picker [data-icon]` | :1429 | none | - | OK |
| 59 | `#layer-form` submit (html:331) | :2135 | POST `.../categories` `{name,color,icon,staffed:bool}` | 1139 | OK |
| 60 | role row `data-rname` | `bindSaveAll` :1831 | - | - | OK |
| 61 | `#role-save-all` (html:370) | btn.onclick | POST `.../roles/{key}` `{name}` | 1235 | OK |
| 62 | role row `[data-rdel]` | :1820 | POST `.../roles/{key}/delete` `{}` | 1218 | OK |
| 63 | `#role-form` submit (html:374) | :1859 | POST `.../roles` `{name}` | 1205 | OK |
| 64 | leader row grip drag / arrows | `bindReorder` :1702 | POST `.../leaders/reorder` `{keys:str[]}` | 1268 | MISMATCH TR2-7 |
| 65 | leader row `data-dname` / `#leader-save-all` (html:408) | `bindSaveAll` :1712 | POST `.../leaders/{key}` `{name}` | 1301 | OK |
| 66 | leader row `[data-ddel]` | :1723 | POST `.../leaders/{key}/delete` `{}` | 1282 | OK |
| 67 | `#leader-form` submit (html:412) | :1843 | POST `.../leaders` `{name}` | 1254 | OK |
| 68 | `#tracking-on` change (html:442) | :1570 | POST `.../tracking` `{enabled:bool}` (GET on tab load :1531) | 1054 / 1046 | OK |
| 69 | roster row `[data-edit]` | :2189 -> `editRoster` | none | - | OK |
| 70 | roster row `[data-delr]` | :2191 | POST `.../roster/delete` `{station_key}` | 1326 | MISMATCH TR2-5 |
| 71 | `#roster-form` submit (html:467) | :2225 | POST `.../roster` `{station_key,original_station_key,display_label,category,operator_name,expects_aprs:bool,poi_id:int\|null}` | 1315 | OK |
| 72 | `#roster-cancel` (html:494) | :2215 | none | - | OK |
| 73 | link row `[data-copy]` | :2310 | none (clipboard) | - | OK |
| 74 | link row `a.icon-btn` open | static href `/e/{slug}/{token}` | GET | 1560 | OK |
| 75 | link row `[data-label-for]` change | :2331 | POST `.../links` `{action:'label',token_id:int,label}` | 1353 | MISMATCH TR2-5 |
| 76 | link row `[data-revoke]` | :2339 | POST `.../links` `{action:'revoke',token_id:int}` | 1353 | MISMATCH TR2-5 |
| 77 | role group `[data-add]` | :2320 | POST `.../links` `{action:'add',role}` | 1353 | MISMATCH TR2-5 |
| 78 | role group `[data-reissue]` | :2350 | POST `.../links` `{action:'reissue',role}` | 1353 | MISMATCH TR2-5 |
| 79 | user row `[data-pw]` | :2392 (prompt) | POST `/api/setup/users/{id}` `{password}` | 1501 | OK |
| 80 | user row `[data-toggle]` | :2401 | POST `/api/setup/users/{id}` `{is_active:bool}` | 1501 | OK |
| 81 | user row `[data-delu]` | :2409 | POST `/api/setup/users/{id}/delete` `{}` | 1529 | OK |
| 82 | `#us-role` change (html:529) | :2431 -> `renderUserEvents` | none | - | OK |
| 83 | `#user-form` submit (html:517) | :2433 | POST `/api/setup/users` `{username,display_name,password,role,organization_id?:int,event_ids:int[]}` | 1474 | OK |
| 84 | startup IIFE / `pollVersion` (visibility + 5 min) | :2474, :1639 | GET `/api/setup/session` | 617 | OK (see TR2-3) |
| 85 | tab loaders: orgs/events/users/course/courses+stations/layers+roles+leaders/tracking/roster/links | :255-275 | GET `/organizations`, `/events`, `/users`, `.../staged` (+`.../categories`), `.../courses` (+`.../categories`), `.../categories`, `.../tracking`, `.../roster`, `.../links` | 1400, 739, 1453, 839, 1096, 859, 1046, 977, 1338 | OK |
| - | (no control) | - | POST `/api/setup/password` | 713 | DEAD route (TR2-12) |
| - | (no control) | - | `event_ids` branch of POST `/api/setup/users/{id}` | 1521 | DEAD branch (TR2-12) |
| - | (no control) | - | `center_lat/center_lon/zoom` on event create/update | 758/779 via admin.py:95,110 | DEAD keys from the UI (TR2-8) |
