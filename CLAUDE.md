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
Open work is tracked as GitHub issues: #1 GPX import, #2 replay,
#3 map tiles, #4 per-organization backup, #5 multi-tenant hosting.
Issues #3-#5 are triggered by hosting a SECOND organization, not the first.

**Starting a fresh session?** Read `docs/PLAN.md` first - it carries the
decisions and the constraints discovered so far. The "Domain rules" section
below is the short list of things that cost real time when violated.

## Status

Phases 1-6 complete, plus a browser setup application. APRS-IS ingest,
KML/KMZ import, the live map with role-gated access, the NCS panel, course-relative
position, incidents, lead runners, and `/setup` for organizations, events,
courses, roster, links and administrators. Repo: private, `MikeWills/CourseOps`.

**Setup is done in the browser**, not the CLI. Only two things stay in a
terminal: the callsign in `.env`, and `courseops serve`.

Phases: 1 ingest ✅ · 2 KML import ✅ · 3 live map ✅ · 4 roster/NCS panel ✅ ·
4a What3Words ✅ · 5 course-relative position ✅ · 6 incidents ✅ ·
7 replay (backlogged, issue #2) · 8 deployment ✅

## Commands

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env                                    # then set APRS_CALLSIGN

./.venv/Scripts/python.exe -m pytest -q                 # 257 tests, no network

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
  importer.py     two-phase import: stage for review, then commit assignments
  units.py        metric storage -> US customary display
  progress.py     snap a station onto a course: "mile 14.2 of Full"
  incidents.py    pickups by bib, status workflow, change log
  leaders.py      first male/female per race, from aid station reports
  styling.py      course colors, line styles, draw order
  what3words.py   normalize/validate W3W strings, no API
  access.py       role tokens: ncs writes; liaison + logistics read
  hub.py          per-event fan-out, bounded queues
  web.py          FastAPI: map page, state snapshot, WebSocket
  static/         Leaflet client, no build step, plus the icon set
  discovery.py    pre-event check-in: which SSIDs are actually on the air
  symbols.py      APRS symbols: is this a person or a digipeater?
  admin.py        setup API: events, import, roster, links
  users.py        admin accounts, scrypt passwords, sessions, roles
  static/setup.*  the setup application
  cli.py          courseops entry point
tests/fixtures/packets.txt          packet corpus, `expectation|raw` per line
tests/fixtures/messy_course.kml     synthetic KML with real organizer defects
tests/fixtures/mankato_marathon.kml REAL MapMyRun export, 26.4 mi; the only
                                    realistic course we have - use it for Phase 5
docs/PLAN.md                        plan, decisions, known gaps
docs/RUNBOOK.md                     event-day procedure for the club
```

## Domain rules that are easy to get wrong

These cost real debugging time if violated. Most are load-bearing for event-day
usability, not style preferences.

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
- **Keep medical detail out of incidents.** Bib, location, status, short
  operational note. Narrative descriptions of a runner's condition would make this
  a system storing health information about identifiable people.
- **Everything is event-scoped**, even with one event. `event_id` on every table.
- **No What3Words API.** Paid service, deliberately not integrated. Manual entry,
  shape validation only, KML lat/lon stays authoritative.
- **Import never writes directly to `course` or `poi`.** Files stage as `pending`
  in `import_feature`; a human assigns each one. `suggest()` is advisory and must
  stay conservative — better unassigned than a parking lot filed as an aid station.
- **KML is untrusted third-party input.** It comes from the race organizer and
  will arrive by web upload. Parse with `defusedxml`, keep the KMZ decompression
  and size guards, and never swap back to stdlib `ElementTree.fromstring`.
- **Coordinates are (lon, lat)** in `geo.py`, `kml.py` and GeoJSON — the reverse
  of how everyone speaks. The database stores `lat`/`lon` as named columns.
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
- **Liaison and Logistics are different teams, not one role.** Liaison is
  embedded with Public Safety and Medics; Logistics is in the field doing traffic
  control, cone placement and teardown. Separate links so one can be revoked
  alone. Both read-only in v1 (`WRITE_ROLES`).
- **Sweeps stay visible to Logistics.** The sweep is the back of the pack, so its
  position is what says a road is clear and the cones can come up.
- **Every mutation goes through `require_write()`.** One place enforces role
  permission, so granting a field role write access is a `WRITE_ROLES` change.
  A valid token in a read-only role gets 403; an invalid token still gets 404.
- **Operator initials are a log annotation, never identity.** Free text, kept in
  the browser, truncated server-side. Nothing may start trusting it as auth.
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
- **Order aid stations by course position, never by name.** Greek letters sort
  Alpha, Beta, Delta, Epsilon, Gamma; "Aid 10" sorts before "Aid 2"; place names
  do not sort at all. `CourseIndex.order_along_course()`.
- **The course name always travels with the mile.** Routes share road, so the
  snap is a coin flip there; "mile 14.2" alone would mislead.
- **Mile figures inherit the course geometry's accuracy.** A hand-drawn route
  that cuts corners is shorter than the road. Never silently smooth it.
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
- **CLI output stays ASCII.** Em dashes become mojibake in the Windows console,
  and a club laptop is the target environment.

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
  (distance, point count, gaps) belong in `test_real_course.py`, so a change
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

- **2026-09-02** Fixed: `hidden` did nothing wherever CSS set `display`, so the UI contradicted itself.
- **2026-09-02** Signup no longer auto-signs-in; it routes to sign-in with a confirmation.
- **2026-09-02** Added mtime-based cache busting so an edited .js/.css is never served stale.
- **2026-09-02** Fixed: `serve` printed nothing on startup; added a banner with the setup URL.
- **2026-09-02** Fixed: printed URLs ignored `--port` and pointed at the wrong port.
- **2026-09-02** Tracked the remaining deployment gaps as issues #3, #4 and #5.
- **2026-09-02** Phase 8: Apache reverse proxy, Let's Encrypt and systemd deployment.
- **2026-09-02** Fixed: session cookies were never marked Secure behind a reverse proxy.
- **2026-09-02** Brought README, RUNBOOK and PLAN up to date with the setup UI.
- **2026-09-02** Added the `/setup` UI: organizations, events, visual course review, roster, links.
