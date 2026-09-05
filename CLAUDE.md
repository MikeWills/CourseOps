# Course Ops

Web map for marathon-style events: race courses and aid stations from organizer
KML/KMZ, overlaid with live APRS positions of the ham radio operators supporting
the event. Built to be stood up by a radio club without much effort.

Full plan, phase detail, and **known gaps / open threads**: `docs/PLAN.md`.
Brand, palette and logo decisions: `docs/DESIGN.md`.
Event-day operating procedure: `docs/RUNBOOK.md`.
Deployment behind Apache with TLS: `docs/DEPLOYMENT.md`.
Brand, palette and logo decisions: `docs/DESIGN.md`.
Complete history with the reasoning behind each fix: `CHANGELOG.md`.
Open work is tracked as GitHub issues: #2 replay,
#3 map tiles, #4 per-organization backup, #5 multi-tenant hosting,
#6 tracking non-ham volunteers.
Issues #3-#5 are triggered by hosting a SECOND organization, not the first.

**Starting a fresh session?** Read `docs/PLAN.md` first - it carries the
decisions and the constraints discovered so far. The "Domain rules" section
below is the short list of things that cost real time when violated.

## Status

Phases 1-6 complete, plus a browser setup application. APRS-IS ingest,
KML/KMZ import, the live map with role-gated access, the NCS panel, course-relative
position, incidents, lead runners, and `/setup` for organizations, events,
courses, roster, links and administrators.

Repo: **public**, `MikeWills/CourseOps`. It was private until the organizer's
KML was purged from the history; nothing that arrives from a third party goes
in it.

Deployed at `courseops.wx0mik.radio`, under
`/mnt/volume_nyc3_01/opt/courseops`. Pushing a version tag deploys it; Actions
-> deploy -> Run workflow deploys a branch.

**Setup is done in the browser**, not the CLI. Only two things stay in a
terminal: the callsign in `.env`, and `courseops serve`. The APRS-IS feed is a
switch in the UI and is OFF until someone turns it on.

**Never tested against real APRS traffic.** Every run so far has had the feed
off. That is the largest untested claim here.

Phases: 1 ingest ✅ · 2 KML/GPX import ✅ · 3 live map ✅ · 4 roster/NCS panel ✅ ·
4a What3Words ✅ · 5 course-relative position ✅ · 6 incidents ✅ ·
7 replay (backlogged, issue #2) · 8 deployment ✅ (automatic, on a version tag)

## Commands

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env                                    # then set APRS_CALLSIGN

./.venv/Scripts/python.exe -m pytest -q                 # 484 tests, no network

courseops init-db
courseops add-event marathon2026 "Spring Marathon 2026" --lat 34.73 --lon -86.58
courseops add-station marathon2026 N0CALL-7 "Half-back" --category sweep
courseops add-station marathon2026 KI4HMD-1 "Aid 4" --category aid_station --no-aprs
courseops roster marathon2026          # shows the generated APRS-IS filter
courseops ingest marathon2026          # live; --max-packets N for a smoke test
courseops tail marathon2026 --latest

courseops import marathon2026 course.kmz   # stage for review; additive across files
courseops review marathon2026 --verbose    # suggestions are advisory only
courseops assign-course marathon2026 1 3 --name "Half"   # stitches segments
courseops assign-poi marathon2026 6 --type aid_station --what3words filled.count.soap
courseops discard marathon2026 2 8
courseops courses marathon2026
courseops style-course marathon2026 1 --color "#cc3333" --order 10
courseops post marathon2026 KI4HMD-1 4   # post an operator at aid station 4
courseops set-w3w marathon2026 4 index.home.raft

courseops links marathon2026           # the three role URLs to send out
courseops serve marathon2026           # web server + live APRS-IS ingest
courseops serve marathon2026 --no-ingest   # map only, no APRS-IS connection
courseops list-links marathon2026 / courseops revoke-link marathon2026 <id>
```

Tests never touch the network. Run `ingest` only when you actually want a live
APRS-IS connection.

## Layout

```
src/courseops/
  schema.sql      full domain schema, all tables event-scoped
  config.py       env settings, tiny .env loader (no dependency)
  parser.py       raw APRS text -> PositionReport, via aprslib
  aprsis.py       async APRS-IS client: filter, login, backoff
  db.py           SQLite access
  ingest.py       feed -> parse -> store; on_position hook for Phase 3 WebSocket
  geo.py          haversine, line length, segment stitching; (lon, lat) order
  kml.py          KML/KMZ parsing, hardened; classification is advisory only
  gpx.py          GPX tracks/routes/waypoints into the same features as KML
  importer.py     two-phase import: stage for review, then commit assignments
  units.py        metric storage -> US customary display
  progress.py     snap a station onto a course: "mile 14.2 of Full"
  incidents.py    pickups by bib, status workflow, change log
  report.py       the after-event page for the race lead: counts, notes, no names
  leaders.py      first male/female per race, from aid station reports
  categories.py   per-event place layers and role names; the `staffed` flag
  styling.py      course colors, line styles, draw order
  what3words.py   normalize/validate W3W strings, no API
  labels.py       one or two characters for a pin, derived from the name
  build.py        which commit is running; "" rather than failing without git
  resources.py    paths to shipped files; correct inside the frozen build
  access.py       role tokens: ncs writes; liaison + logistics read
  hub.py          per-event fan-out, bounded queues
  web.py          FastAPI: map page, state snapshot, WebSocket
  static/         Leaflet client, no build step, plus the icon set
  static/icons.js shared glyph set, inline SVG, used by map and setup
  discovery.py    pre-event check-in: which SSIDs are actually on the air
  symbols.py      APRS symbols: is this a person or a digipeater?
  admin.py        setup API: events, import, roster, links
  users.py        admin accounts, scrypt passwords, sessions, roles
  static/setup.*  the setup application
  cli.py          courseops entry point
tests/fixtures/packets.txt          packet corpus, `expectation|raw` per line
tests/fixtures/messy_course.kml     synthetic KML with real organizer defects
tests/fixtures/consumer_export_course.kml  synthetic, but with a real export's
                                    defects: duplicate points, straight-line
                                    gaps, identically named placemarks.
                                    Regenerate with tools/make_course_fixture.py
docs/PLAN.md                        plan, decisions, known gaps
docs/RUNBOOK.md                     event-day procedure for the club
```

## Domain rules that are easy to get wrong

These cost real debugging time if violated. Most are load-bearing for event-day
usability, not style preferences.

- **The feed is OFF unless someone turns it on, and that is a privacy
  decision.** The buddy filter asks for each operator's callsign wherever they
  are, not only on the course, so a feed left running records where volunteers
  live and work. `event.ingest_enabled` is persisted rather than held in
  memory: a deploy restarts the service, and a feed that failed to come back
  mid-event looks exactly like a quiet net.
- **Turning tracking on must start exactly one connection.** `start_ingest`
  stops any other feed first - APRS-IS bans clients that open several - and is
  a no-op if that feed is already running, so a double press cannot double the
  connections. Refuse to enable without a usable callsign rather than starting
  a task that dies: a switch reading "on" with nothing behind it is worse than
  no switch.
- **Nothing tags a release automatically.** The deploy workflow REACTS to a
  tag; it never creates one. Bump `pyproject.toml` and `__init__.py`, commit,
  then tag - the release workflow refuses a tag that disagrees with the
  packaged version, because v0.1.1 once shipped saying 0.1.0.
- **Receive-only, forever.** Passcode `-1` grants read access and no transmit
  capability. The club needs a callsign and no secret. Never add a real passcode.
- **One APRS-IS connection for the whole server.** Browsers connect to our
  WebSocket; the server fans out. APRS-IS bans clients that open many connections
  or reconnect tightly. Keep the backoff and the jitter.
- **The roster is not the APRS feed.** Three separate things: the aid station (a
  location from KML), the operator assigned to it (a callsign), and a position
  report (only if they beacon). **Most aid station operators never beacon.**
- **`expects_aprs=0` means "do not alert when silent", not "discard".** It gates
  staleness alerting and filter construction only. Use `tracked_station_keys` to
  build the filter and `all_station_keys` to decide whether to store. Getting this
  backwards was already a bug once.
- **Two independent status axes.** Operational (`pending`/`active`/`closed`, set
  by NCS) and radio (`fresh`/`stale`/`silent`/`n/a`, derived). Never merge them
  into one badge — "On station, no APRS" is healthy; "Rolling, silent 18 min" is
  an alarm.
- **SSID is part of station identity.** `N0CALL-9` and `N0CALL-7` are different
  radios on different people. Never key on the base callsign.
- **Symbol table and code travel as a pair.** The table character changes the
  meaning of the code.
- **Store metric, display US customary.** aprslib gives km/h and meters; that is
  what goes in SQLite. Convert only in `units.py` at the display edge.
- **Never interpolate marker movement.** Updates arrive every 1-5 minutes with
  gaps. Showing a position that was never reported is worse than showing a stale
  one, because someone will act on it. Sparse jumps are correct.
- **Switch state is carried by knob POSITION, colour only reinforces it.**
  Green/red is the worst pair for red-green deficiency and washes out in
  sunlight. Never make colour the only cue for any state in this app.
- **Surface silent failures in the UI, not in a command.** A wrong SSID makes
  someone invisible with no error; `ssid_alerts` puts it in front of NCS
  unprompted. Anything that depends on remembering to run a check will be
  forgotten on race morning.
- **`station_exclusion` must hide stored positions too**, not just gate ingest -
  otherwise "ignore" leaves the thing on the map.
- **The filter is a WILDCARD per callsign (`b/WX0MIK*`), not per SSID.** A
  volunteer who signs up as `-1` and beacons `-5` would otherwise be silently
  invisible. Ingest therefore matches on the base callsign too - both halves are
  needed or the wildcard is pointless. The cost is the operator's digipeater
  arriving; `station_exclusion` is how it is dismissed.
- **Buddy filter, not area filter.** We know the roster in advance. This avoids
  incidentally storing the public's location.
- **Lead runner sightings are reports, not measurements.** There is no tracker
  on the front runner. Store the sighting; derive position, pace and ETA from it.
- **Reject an implausible pace rather than publishing it.** NCS enters reports in
  bursts, and two sightings seconds apart yields a 120 mph "pace" and an ETA an
  aid station would plan around. Outside 3:00-30:00 per mile, show nothing.
- **Bib colour is separate from course line colour.** The line colour is a map
  choice; the bib colour is how an operator identifies a runner in front of them.
- **Status history is append-only and cannot be rebuilt later.** `roster.op_status`
  is overwritten on every change; `roster_status_log` is the record. Any new
  overwritten state needs the same treatment, decided before an event, not after.
- **`incident.status_at` is the age of the CURRENT status**, not of the
  incident. "Requested 8 minutes ago, nobody dispatched" is the whole point;
  sorting is status rank then longest-waiting.
- **Never block incident creation on a dialog.** A pickup is called in before
  the bib is known. Create first, fill the bib in after.
- **The report's small maps are drawn by the browser, never fetched by the
  server.** Same Leaflet, same OSM tiles as the live map, so the page adds no
  dependency and sends nothing anywhere the live map does not already. A
  static-map API would be a new third party receiving incident coordinates.
- **The report page carries no names, and times in the EVENT's zone.** It
  goes to the organizer, who needs counts and places, not which volunteer
  reported what. Timestamps are stored UTC and formatted by the browser with
  `Intl.DateTimeFormat` in `event.timezone` - a Windows Python has no zone
  database without a fifth dependency, and the browser always does.
- **Keep medical detail out of incidents.** Bib, location, status, short
  operational note. Narrative descriptions of a runner's condition would make this
  a system storing health information about identifiable people.
- **A one-shot fix, never "press locate first".** Anything that depends on
  remembering a prior step is forgotten on race morning, so `currentFix()`
  falls back to `getCurrentPosition` and reuses the watch's fix only while it
  is under 30 seconds old - a stale fix pins someone where they were, not
  where they are.
- **Everything is event-scoped**, even with one event. `event_id` on every table.
- **No What3Words API.** Paid service, deliberately not integrated. Manual entry,
  shape validation only, KML lat/lon stays authoritative.
- **Import never writes directly to `course` or `poi`.** Files stage as `pending`
  in `import_feature`; a human assigns each one. `suggest()` is advisory and must
  stay conservative — better unassigned than a parking lot filed as an aid station.
- **KML is untrusted third-party input.** It comes from the race organizer and
  will arrive by web upload. Parse with `defusedxml`, keep the KMZ decompression
  and size guards, and never swap back to stdlib `ElementTree.fromstring`.
- **Coordinates are (lon, lat)** in `geo.py`, `kml.py`, `gpx.py` and GeoJSON —
  the reverse of how everyone speaks. The database stores `lat`/`lon` as named
  columns. GPX is the trap: it writes `lat=".." lon=".."` as attributes,
  lat-first, and `gpx.py` is the one place that turns them around.
- **`geo.stitch` must grow the chain at both ends.** Growing only from the tail
  silently folds a course back on itself when the file lists a middle segment
  first. This was a real bug; there is a regression test.
- **Course overlap is solved by draw order, not by dashes.** The Full, Half and
  10K share road; `course.sort_order` decides which line wins (higher draws on
  top) and is adjustable. Courses are solid by default. Dash patterns exist as
  an opt-in for seeing two coincident routes at once.
- **Adding a schema column requires a migration entry.** `CREATE TABLE IF NOT
  EXISTS` skips existing tables, so a new column never reaches an existing
  database. Add it to `_ADDED_COLUMNS` in `db.py` as well as `schema.sql`.
- **`<styleUrl>` can be the only thing distinguishing two placemarks.** MapMyRun
  names the route, the start marker and the finish marker identically. Keep
  `style_id` captured, fed into `suggest()`, and shown in `review` for duplicate
  names.
- **Hint patterns must normalize `_` and `-` to spaces first.** `_` is a word
  character, so `start` never matches inside `start_marker`.
- **SAG is a fourth role, and the only field role that writes.** They work the
  pickup queue from a vehicle: en route, picked up, dropped off, and the bib
  once they can read it. Nothing else.
- **Liaison and Logistics are different teams, not one role.** Liaison is
  embedded with Public Safety and Medics; Logistics is in the field doing traffic
  control, cone placement and teardown. Separate links so one can be revoked
  alone. Both read-only.
- **Sweeps stay visible to Logistics.** The sweep is the back of the pack, so its
  position is what says a road is clear and the cones can come up.
- **Every mutation goes through `require_capability()`.** One place enforces
  permission and each endpoint names what it needs, so widening a role is a
  change to `ROLE_CAPABILITIES`. A valid token lacking the capability gets 403;
  an invalid token still gets 404.
- **Operator initials are a log annotation, never identity.** Free text, kept in
  the browser, truncated server-side. Nothing may start trusting it as auth.
- **`/healthz` is unauthenticated and stays minimal.** Liveness and a version,
  nothing else - there is a test asserting the exact key set. The deployed
  COMMIT in particular belongs behind the login (`/api/setup/session`), because
  it tells anyone who asks precisely which code is running. `build.py`
  establishes it once, cheaply, and returns "" rather than failing when there
  is no git.
- **A script added from Windows is not executable.** `core.filemode` is off
  there, so it commits as 100644 and the failure appears only on the server, as
  "Permission denied" on a file that looks fine in a listing. Use
  `git update-index --chmod=+x`, and there is a test asserting `deploy.sh`
  stays 100755.
- **Nothing in `deploy/` may hardcode the install path.** It is wherever the
  club put it - this deployment lives under a mounted data volume, not
  `/opt`. `deploy.sh` derives `APP_DIR` from its own location; the only places
  a real path must be written are the `command=` restriction in
  `authorized_keys` and the sudoers line.
- **`git checkout <branch>` on the server does not deploy that branch.** The
  working copy is on a detached HEAD, so a local branch there is whatever it
  was at clone time: the deploy reports success and installs stale code.
  `deploy.sh` resolves to a concrete commit first - tag, then
  `origin/<branch>` - and refuses an unknown ref rather than deploying
  whatever HEAD happens to be.
- **Behind a proxy the app cannot see the real scheme.** Run with
  `--behind-proxy` or session cookies silently lose the Secure flag. Bind
  127.0.0.1 so TLS cannot be bypassed.
- **Apache needs `mod_proxy_wstunnel` and /ws/ rules BEFORE the catch-all.**
  Otherwise the map loads and then never moves, with no visible error.
- **NEVER modify `.env`.** It is the user's file and holds their callsign. To
  test configuration behaviour, set the environment variable for that one
  command (`APRS_CALLSIGN=... courseops ...`) - never edit the file, and never
  "revert" it to the placeholder, which silently destroys a real value.
- **Setup belongs in the UI.** The premise is that a club can stand this up
  without much effort; only `.env` and starting the server may stay in a
  terminal. The CLI is kept for scripted setup and for tests.
- **Organizations are the tenancy boundary.** Every event belongs to one, and
  `may_access_event` is the single place access is decided. It checks the
  organization BEFORE any per-event assignment, so a stale assignment after a
  club change grants nothing.
- **Volunteers keep bearer links; only admins get accounts.** A link can be
  re-sent to someone whose phone died at 6am by anyone holding it.
- **An invalid token returns 404, never 403.** A 403 would confirm the event
  exists. Tokens are also scoped to their event: valid elsewhere means nothing.
- **Never interpolate marker movement in the client** (same rule as the plan).
  `setLatLng`, not an animated transition.
- **On a phone the role's sections go to the TOP of the sheet instead.** Same
  list as the side panel, because it is the same decision: there is no second
  panel at that width, so without this the one thing a role exists to look at
  opens below the fold. `desiredSheetOrder(wide)` takes the width as an
  argument rather than reading the media query, so the phone ordering can be
  checked without a phone - which is the only way it gets checked at all here.
- **What goes in the right-hand panel depends on the ROLE, not just width.**
  `SIDE_PANEL_BY_ROLE` in `app.js`: NCS and Logistics get stations, Liaison and
  SAG get the pickup queue. Liaison is with Public Safety and Medics and needs
  to see who needs picking up; Logistics needs the sweep, because that is what
  says a road is clear. Sections moved out must go back into the AUTHORED
  order, not onto the end - `sheetOrder` records it once at startup.
- **Three layout tiers, not two, and the JS must agree with the CSS.**
  < 700px bottom sheet; >= 700px the sheet is a sidebar (this is where tablets
  in portrait land - an 11" iPad is 834 CSS px); >= 1000px the side panel too,
  because below that two panels leave a strip of map too narrow to read a
  course on. `SIDEBAR` and `WIDE` in `app.js` mirror those numbers: if they
  drift apart a section ends up invisible, or in a panel that is not rendered.
  Panel widths are `clamp()`, not fixed, for the same reason.
- **Layer switches live at the bottom of the panel.** Set once before the race
  and never touched, while everything above them is read all day. Anything new
  that is read during the event goes above them, not below.
- **The stations section is ONE element that lives in two places.** On a wide
  screen it moves into the right-hand panel; below 860px it moves back into the
  sheet. Moved, never duplicated - two copies drift the moment one is
  re-rendered, and the stale one is the one someone reads.
- **A panel that can be hidden needs a visible way back.** Otherwise it is lost
  for the whole event. Folded sections and hidden panels are both remembered in
  prefs, because a phone reloads on its own coming back from a dead zone and
  re-opening what someone deliberately folded reads as the screen not working.
- **The role badge must never compete with the connection badge.** It sits
  next to the one thing in the top bar that says whether a phone is showing
  stale data, so it is outlined rather than filled and carries no status
  colour: nothing about which role you are is an alarm.
- **A full-screen panel on a phone still stops below the top bar.** The
  connection badge is there, and it is the only thing that says a phone is
  showing stale data - covering it hides exactly what someone needs to check
  before acting on what the panel says.
- **The sheet's drag grip is not a close button.** It was 42x5 pixels, and
  while the sheet was open it was the ONLY way out, because the toggle sits
  under the sheet at a lower z-index. Any control that closes something needs
  the 44px target like everything else.
- **A floating map control must be anchored to the MAP's edge, not the
  window's.** `#locate-btn` is z-index 500 and `#stations-panel` is 550, so
  `right: 12px` put it underneath the panel on every screen over 1000px -
  invisible, with no error, on the layout NCS actually uses. Offset by the same
  `clamp()` the map is, and put it back under `body.stations-hidden`.
- **`body` is `position: fixed`. `overflow: hidden` is not enough on iOS.**
  WebKit scrolls the document to anything reaching past the viewport - the
  closed sheet, translated 102% below - and does so on its own when the app
  comes back from the background. The symptom is the header gone off the top
  and the "closed" panel showing along the bottom. The closed sheet is also
  `visibility: hidden`; the sidebar tier sets it visible again because a
  sidebar is never closed. "iOS" here means every browser on it - Chrome and
  Firefox on iOS are WebKit underneath, by Apple's rule - so there is no
  browser to switch to.
- **`#map` is its own stacking context (`isolation: isolate`). Keep it.**
  Leaflet numbers its controls 1000 and its popups 700, and without isolation
  those compete with the top bar (500), the stations panel (550) and the sheet
  (700) directly - the zoom buttons floated over the open panel on a phone.
  Isolated, nothing inside the map can draw over anything laid on top of it,
  whatever z-index Leaflet picks next.
- **The connection badge must stay visible.** It is the only signal that a phone
  is showing stale data. Leaflet's zoom control shares that corner at z-index
  1000; the top bar reserves space for it.
- **`Subscription` needs `eq=False`.** Subscriptions live in a set, and two
  browsers on one event are distinct subscribers with identical fields.
- **Client escaping goes through `escapeHtml`,** which escapes quotes too - the
  textContent/innerHTML trick does not, and values land in attributes.
- **Geolocation needs a secure context.** Browsers block it over plain http://
  except on localhost. This constrains deployment (Phase 8): a club serving over
  a LAN without TLS loses the "where am I" dot. The client names the real cause.
- **The viewer's own position is local only** - never sent to the server, never
  stored, never visible to other viewers.
- **"Ops" in Course Ops is the product name, not the Logistics team.** Roles are
  NCS / Liaison / Logistics.
- **Navy is chrome; the map stays light.** Field roles read it outdoors for six
  hours. Never theme the map surface dark without a user toggle and dark tiles.
- **No mile figure beats a wrong one.** A station further than
  `progress.DEFAULT_MAX_OFFSET_M` from every course reports `None`, and the
  client shows the callsign instead. Someone acts on this number.
- **Geometry cannot order places once an event has more than one route.**
  Each place snaps to whichever course line is nearest, and where routes share
  pavement that is a coin flip - so distances from different races interleave
  into one meaningless list. `poi.sort_order` is the club's own order, set by
  dragging in setup, and it wins. 0 means "never placed by hand" and sorts
  LAST, so an unordered event is unchanged and a later import lands at the end
  where it is visible. The lead runner progression uses the same order, or
  "the next station" disagrees with the list the club just arranged.
- **Which race a stop serves is stated, not guessed.** `poi_course`, many to
  many, because one water stop routinely serves several races - the organizer's
  file names them "WATER (ALL)". Snapping a stop to the nearest course line
  picks exactly ONE, so every stop closer to another route's line was dropped
  from this race's progression and the "Passed X" button skipped from D to I.
  Nothing stated falls back to the snap, which is what keeps existing events
  working; never make that fallback the primary path again.
- **Undo is not a reset.** `undo_last_sighting` removes one report, for a
  mis-tap mid-race. `clear_sightings` empties a race and division, for the
  morning of the event when the panel is carrying a rehearsal. Both are scoped
  to one course and division, so clearing a race that has not started cannot
  take out one that has.
- **A lead runner sighting may be at ANY station.** The picker offers all of
  them on purpose, because which race a stop belongs to is inferred from
  proximity. So anything that NAMES a sighting must be built from every staffed
  place, not from one course's stations - building it per course meant a
  correction was stored and then displayed as nothing, and the leader looked
  stuck. There is no "finished" state to reset: position is only ever the last
  sighting, so a later sighting is the correction.
- **Order aid stations by course position, never by name.** Greek letters sort
  Alpha, Beta, Delta, Epsilon, Gamma; "Aid 10" sorts before "Aid 2"; place names
  do not sort at all. `CourseIndex.order_along_course()`.
- **The course name always travels with the mile.** Routes share road, so the
  snap is a coin flip there; "mile 14.2" alone would mislead.
- **Mile figures inherit the course geometry's accuracy, and that is
  accepted.** A hand-drawn route that cuts corners is shorter than the road.
  Never smooth, snap or re-measure it: the line is there to see the route, and
  the organizer's mile-marker places carry the real distances.
- **Icons: regenerate with `python tools/make_icons.py`, never by hand.** iOS
  ignores SVG and the manifest for home screen icons; Android crops maskable
  icons to the central 80%. Full-bleed sources keep square corners because both
  platforms apply their own mask.
- **The manifest is per-role and dynamic.** The app has no tokenless entry
  point, so a static `start_url` installs a shortcut to a 404.
- **Brand orange never appears inside a station row.** Amber and red mean
  something there. Status colour only ever appears on status. Use `--orange-ink`
  for orange text on white; `--orange` fails contrast at 2.87:1.
- **`hidden` only hides if `[hidden] { display: none !important; }` is set.**
  Any class that sets `display` - `.field{flex}`, `.gate{grid}`, `.row`,
  `.check` - has equal specificity to the browser's `[hidden]{display:none}`
  and comes later, so it wins and `el.hidden = true` does *nothing*. The
  symptom is not an error but an interface that contradicts itself: a sign-in
  form showing while the header says you are signed in. The rule lives in
  `app.css`; never remove it, and never hide by setting `style.display`.
- **An event-scoped tab must hide its form, not just warn over it.** A banner
  above a live form still lets someone fill it in and post to
  `/events/null/...`. `gateOnEvent()` hides the panel body, and restores only
  what it hid - elements hidden for their own reasons must stay hidden.
- **A roster entry may be a bare callsign; `bound_key` is the SSID heard.**
  Anything joining a roster row to a position must go through
  `db.tracking_key()`, or a bare entry and its own packets show as two stations
  - one silent forever, one unattributed. Writes accept either key via
  `db.resolve_station_key()`. Never rewrite `station_key` to the heard SSID:
  what a human typed has to survive, and the status log must stay on one key.
- **Bind only to symbols that say person.** The wildcard filter drags in the
  operator's own digipeater and igate. Binding an aid station to their home
  igate parks that person on the map at their house all day - confidently, and
  wrongly. `symbols.is_infrastructure` gates it.
- **An event's slug is immutable; it is in every link already sent.** Renaming
  the event changes the displayed name only. Changing `/e/<slug>/<token>` would
  404 every volunteer holding a link, silently, on the morning they need it.
- **Glyphs are fine; reach for inline SVG when the icon carries colour or
  state.** A Unicode glyph ships nothing and needs no markup, so it is a
  reasonable choice for a plain icon. The two things it cannot be trusted with:
  a codepoint that renders in *emoji presentation* ignores `color` entirely and
  arrives as decoration (U+270E with the U+FE0E text-presentation selector
  still came out a full-colour pencil in Chrome on Windows), and glyph metrics
  vary by font, so precise sizing against a 34px button is guesswork. Anything
  that must redden with a danger button, invert when pressed, or take a
  club-set layer colour is SVG, because a path inherits `currentColor`. Check a
  candidate glyph in Chrome on Windows before committing to it - that is where
  the emoji substitution bites. An icon font stays out: another file to ship
  and to fail to load, and the frontend has no build step.
- **Every icon-only button needs both `title` and `aria-label`.** The label
  names the row, not just the verb: "Delete Aid 3", never "Delete".
- **Permission is per capability, not one write flag.** `ROLE_CAPABILITIES` in
  `access.py` is the whole policy; each endpoint names what it needs via
  `require_capability`. Never widen a field role by adding it to a second place.
- **Reporting an incident and working the queue are different capabilities.**
  `CAP_INCIDENT_REPORT` (all four roles) opens a pickup or a note and fills in
  its bib, note and position; `CAP_INCIDENTS` (NCS and SAG) moves one along and
  deletes it. Everyone is somewhere incidents happen, and relaying a report to
  whoever holds the right link is how it arrives late. But the queue and its
  count are read as "who is still waiting", so closing or deleting an entry is
  the power to make that count lie - and the widely-handed-out links must not
  have it.
- **The map tap is the primary way to drop a pin; "Here" is the shortcut.**
  Liaison sits at the EOC reporting places they have never seen, so their own
  location is the one position that is always wrong. Never make "Here" the
  default, and never remove the tap.
- **"Here" is the ONLY thing that sends the viewer's own position.** One fix,
  on one press, stored as an incident's location - not continuous tracking,
  which is why it is a button and not a background behaviour. The locate dot
  stays local. A fix worse than 100 m still places the pin (a rough position
  with a note beats no report) but the accuracy is stated, or someone reads
  wifi triangulation as GPS.
- **An incident is deleted, never "cancelled".** The pickup queue and its
  count are read as "who is still waiting", so a status that has to be filtered
  out of both is a second chance to get that filter wrong - which this app got
  wrong once already with notes. Deleting publishes the removal, because every
  other browser holds the row AND a map marker and nothing will mention it
  again. `incident_log` cascades: it is the history OF an incident, not an
  audit trail that outlives it.
- **A course note is not a pickup.** `incident.kind` separates them. The pickup
  queue and its count are read as "who is still waiting", so a note must never
  appear there. Notes have no status workflow; their audience is the organizer
  after the event.
- **"Picked up" is not "dropped off".** In the vehicle still counts as
  outstanding; delivered does not. `incidents.waiting_count` is the number NCS
  glances at, and it treats picked-up as still waiting on us.
- **Proximity sort never overrides status.** Sorting purely by distance buries a
  pickup that has waited twenty minutes. Status leads; distance breaks ties
  within it. The distance is straight-line and labelled "away" - there is no
  routing engine, and it must never read as an ETA.
- **The taxonomy is the club's, not the code's.** Place layers live in
  `poi_category`, one per event, unlimited. Never reintroduce a hardcoded list
  of place types - it was in five places and every one of them meant editing
  Python to accept a race.
- **`staffed` is the flag everything operational keys off.** Not
  `poi_type == 'aid_station'`. Who can be posted somewhere, where a lead runner
  can be sighted, which places are worth a What3Words address. Getting this
  wrong is silent: the club renames a layer and a feature quietly empties.
- **Station roles are open too, not a fixed seven.** A club fields roles the
  defaults lack - Liaison being the obvious one - and the list was closed in
  two places at once: `roster_roles` filtered its own rows against
  DEFAULT_ROSTER_ROLES on the way out, so an added role could never appear
  anywhere. An added role has no entry in `db.OP_STATUS_LABELS` and falls back
  to the generic wording, which is why that fallback exists.
- **Seed defaults ONLY into an event that has none.** True of place layers and
  now of roles: seeding on every read resurrects what a club deleted. Getting
  this wrong is invisible until someone deletes something and it comes back.
- **A layer's key never changes; its name is free.** `poi.poi_type` holds the
  key, so renaming is display-only and no place has to move. Same for station
  roles, whose keys carry their status vocabulary.
- **Never resurrect a deleted default.** Seed defaults only into an event with
  no categories at all. A layer that reappears after being deleted teaches
  people not to trust the screen.
- **Refuse to delete a layer that still has places.** They would stay in the
  database, vanish from the map, and nothing would say why.
- **Every setup change publishes a resync.** One middleware on
  `POST /api/setup/events/{id}/...`, never per-endpoint - a renamed station has
  to reach the field, and the failure is silent because NCS sees their own
  screen update. Resync reloads data, not the page: view and layer choices are
  restored only on first load, so they survive.
- **Verify setup instructions by cold-starting a clean clone into an empty
  virtualenv.** A missing dependency (`python-multipart`) that this machine
  happened to have made the app fail to boot for everyone else, and no test
  caught it because tests run in the developed environment.
- **Organizer KML is one flat list, not a folder per kind of place.** The real
  Mankato export has no `<Folder>` elements, so everything imports into one
  layer and a club sorts it afterwards - which is why the Places table can move
  a selection in bulk. Never assume points arrive pre-sorted.
- **Declare literal routes before parameterised ones.** `/pois/move` after
  `/pois/{poi_id}` is never reached: FastAPI matches in order, "move" parses as
  an id, and the UI button silently does nothing.
- **GIS exporters put their attribute table in `<description>` as HTML, not in
  `ExtendedData`.** For an ArcGIS file it is the only thing distinguishing a
  water stop from a mile marker - every placemark being named after its race.
  `kml.attributes_from_description` reads it, and `Type` beats every text hint
  because the file is stating what the thing is.
- **A placemark's `<description>` never reaches `poi.notes` raw.** For an
  ArcGIS file it is an entire HTML document, and it was showing verbatim in
  the popup on a phone. `kml.description_notes` is the one place that decides
  what a description is worth: nothing for an exporter's attribute table
  (already consumed at staging), stripped text for a hand-written one.
  `db._clean_html_notes` catches events imported before that existed.
- **Import may create layers; it may never create places.** The exporter has
  already decided the layers, so creating them saves retyping. Places still
  stage as pending for a human, which is what stops a parking lot filing itself
  as an aid station.
- **A suggestion must name a layer that exists.** Otherwise assignment is
  refused and the UI just looks broken - which is why `END` is aliased onto the
  `finish` layer that ships by default.
- **`static/` must be in `package-data`.** The frontend has no build step, so
  those files ARE the app. A wheel without them installs something whose every
  page 404s, and `pip install -e .` hides it completely because it reads the
  source tree. Build a wheel and look inside before trusting a release.
- **Anything that resolves a path to a shipped file goes through
  `resources.py`.** `Path(__file__)` is wrong inside the frozen Windows build,
  where the package is unpacked to a temporary directory that does not contain
  the data files - and the symptom is a page with no stylesheet rather than an
  error.
- **A frozen build must not write beside itself.** It is run from a Downloads
  folder or a USB stick; the database goes to `%LOCALAPPDATA%`.
- **A callsign is required where it is USED, never at startup.** Everything
  except the live APRS-IS connection works without one, and refusing to boot
  over it turns the Windows download into a console window that flashes and
  vanishes.
- **Never reload a table that holds unsaved edits.** A per-row save that
  re-renders the whole list silently discards every other edit in progress -
  it cost a real user twelve renames. One save button for the whole table,
  which sends only the fields that actually changed. `bindSaveAll` in
  `setup.js` is the one implementation; wire a new editable table to it rather
  than writing a fourth copy, and never add a second save button at a
  different scope - two scopes is how the original bug happened.
- **Two tables on one tab must re-render independently.** Layers and roles
  share an endpoint, and reloading both after saving either wiped edits from a
  table the user had not touched - the same bug, one table over. `loadLayers`
  takes which half to re-render.
- **A pin label is never the first letter of the name.** Clubs number stations
  as often as they letter them, so "Aid 1/2/3" through a first-letter rule
  labels the entire course "A" - a feature that looks like it works and conveys
  nothing. `labels.derive` takes a number if the name has one, then the first
  word that is not naming the kind of place ("aid", "water", "stop") or the race
  ("ALL", "FULL"). Real organizer files are full of `WATER (ALL)`.
- **The pin label is derived, never stored.** `poi.label` is an override only,
  and is discarded when it matches the guess - otherwise renaming a station
  leaves its pin reading the old character. Two characters is the ceiling: the
  marker is 24px, 30px when labelled.
- **A labelled pin gives up its glyph, so labelling is per layer.** Both do not
  fit at marker size. One layer labelled and the rest glyphed is the working
  arrangement; labels default on for `staffed` layers only, because the same
  switch across 48 mile markers buries the map in digits.
- **Labelled markers need a `zIndexOffset`.** Leaflet stacks markers by
  latitude, so an unlabelled pin a few metres north covers the character
  someone turned labels on to read.
- **A new column whose sensible default comes from existing data needs a
  backfill.** `ALTER TABLE` takes only a constant, so the flag arrives off for
  every event that already exists - invisible to exactly the people with data.
  `_BACKFILL` in `db.py` runs alongside `_ADDED_COLUMNS`.
- **CLI output stays ASCII.** Em dashes become mojibake in the Windows console,
  and a club laptop is the target environment.

## Working method

**Changes reach `main` through a pull request, never a direct push.** Branch,
commit, push the branch, open a PR, let CI finish.

This is not ceremony on a one-person project. It is the only place where the
whole change is visible at once instead of arriving as a sequence of commits
nobody re-reads, and CI runs before `main` is affected rather than after. The
tests are the safety net for behaviour; the PR is the safety net for judgement.

- Branch names say what and why: `fix/stitch-invents-a-leg`,
  `feat/sag-role`, `chore/pull-request-workflow`.
- One PR per idea. A PR that fixes a bug *and* renames a module is two reviews
  pretending to be one.
- The PR description carries the reasoning; commit messages still carry the
  detail. Neither is a substitute for the CHANGELOG entry.
- Do not merge on red. If CI fails, the branch is the right place for that.

## Documentation discipline — do this every cycle

Documentation is maintained as work happens, not batched up at the end. Batched
docs get skipped, and the reasoning behind a decision is unrecoverable a week
later. **Before considering any change complete, update every file below that it
touches.** This is not optional polish; it is how the next session (or the next
person) avoids re-deriving what was already settled.

| File | Update when | What goes in it |
|---|---|---|
| `CHANGELOG.md` | **every** change | Full entry under `## [Unreleased]`, newest first. Added / Changed / Fixed. For a fix, say what broke and **why it mattered** — not just what was edited. |
| `CLAUDE.md` | every change | The "Recent changes" list — one line per entry, trimmed to exactly 10. Plus a new "Domain rules" bullet if the change revealed a trap. Plus the test count and status line if those moved. |
| `docs/PLAN.md` | a *decision* changes | Phase detail, resolved questions, known gaps. If the user settles a question in conversation, it lands here — conversation is not storage. |
| `docs/RUNBOOK.md` | operator-visible behavior changes | New command, new failure mode, new thing a volunteer must do on event day. |
| `README.md` | user-facing behavior changes | Setup, commands, what the thing does. |
| GitHub issues | work deferred, not done | Anything discovered but out of scope now. Reference the issue number in `docs/PLAN.md` known gaps. |

Rules that keep this honest:

- **A discovered constraint is documentation.** If something turns out to be
  impossible, paid, blocked by a browser, or true only of one file format, it
  goes in `docs/PLAN.md` known gaps immediately — even with no code change.
- **Record why, not just what.** "Fixed stitch bug" is worthless in six months.
  "Growing the chain only from the tail folded the course back on itself" is
  what stops it being reintroduced.
- **A real-data finding gets a test, not a note.** Facts about a real file
  (distance, point count, gaps) belong in `test_exported_course.py`, so a change
  that breaks the assumption fails loudly rather than drifting.
- **Never let CLAUDE.md hold content.** It is an index and a rule list. Detail
  lives in `docs/`; history lives in `CHANGELOG.md`.
- **Keep CLI output and docs in step.** If a command's flags change, the README
  and runbook examples change in the same commit.

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses for value types.
- Dependencies stay minimal — every added package is one more thing a club has to
  install. Four runtime dependencies today (`aprslib`, `defusedxml`, `fastapi`,
  `uvicorn`), each with a stated reason; justify any addition. The frontend has
  no build step on purpose - no npm in a club's deployment.
- Comments explain *why*, especially where a choice looks arbitrary but is
  protecting against a real event-day failure.
- New parser edge cases get a line in `tests/fixtures/packets.txt`, not a bespoke
  test. Captured live traffic goes in `live_*.txt`, which is gitignored — it holds
  the positions of people who did not consent to a public repo.

## Recent changes

Last 10 entries; full record in `CHANGELOG.md`.

- **2026-09-05** After-event report page for the race lead: pickups counted, notes listed, no names (#7).
- **2026-09-05** GPX course import: tracks, routes and waypoints through the same review as KML (#1).
- **2026-09-04** Fixed: returning to the app on a phone could scroll the header away and show the closed panel.
- **2026-09-04** Fixed: a place's popup showed the organizer's whole HTML document as its notes.
- **2026-09-04** Fixed: on a phone the zoom buttons covered the header and floated over the open panel.
- **2026-09-04** Every role can report a pickup or course note; only NCS and SAG work the queue.
- **2026-09-04** "Here" drops the pin at your own location, beside the map tap rather than replacing it.
- **2026-09-04** The locate button is a crosshair, and no longer hidden behind the stations panel.
- **2026-09-04** Live tracking is a switch in setup, off by default, with the roster's filter shown.
- **2026-09-04** The running build is shown in the setup header, so a deploy can be confirmed.
