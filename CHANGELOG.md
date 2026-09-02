# Changelog

All notable changes to Course Ops. Newest first. The ten most recent entries
are mirrored into `CLAUDE.md`; this file is the complete record.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### 2026-09-02 - Backlog captured as issues

The deployment guide had grown a "still open" section that was becoming a second
issue tracker. Moved to GitHub so it is triaged in one place.

- [#3](https://github.com/MikeWills/CourseOps/issues/3) - own map tiles before
  hosting a second organization. Notes that the tile style is not just a URL:
  the palette assumes light tiles legible in daylight, so a darker style needs
  the colours rechecked, and attribution changes with the source.
- [#4](https://github.com/MikeWills/CourseOps/issues/4) - per-organization
  backup, export and offboarding. Flags that administrator accounts and access
  tokens must **not** travel with an export, since both are install-scoped
  credentials.
- [#5](https://github.com/MikeWills/CourseOps/issues/5) - resource limits, a
  signup path and static asset caching. Flags that there is no password reset
  and no email capability at all, which is a real dependency decision rather
  than a small change.

`docs/DEPLOYMENT.md` now points at the issues instead of restating them, and
records that all three are triggered by hosting a second organization rather
than by running the first event.

### 2026-09-02 - Phase 8: deployment behind Apache

#### Fixed
- **Session cookies were never marked Secure behind a reverse proxy.** The app
  is spoken to in plain HTTP on localhost, so `request.url.scheme` was always
  "http" and the flag was skipped in exactly the deployment where it matters.
  `courseops serve --behind-proxy` now has uvicorn honour X-Forwarded-Proto,
  with `--trusted-proxy` (default 127.0.0.1) deciding who may set it - without
  that, any client could simply claim HTTPS.

#### Added
- `deploy/apache-courseops.conf` - vhost with TLS, the ACME challenge left
  unproxied so renewal works, and **WebSocket proxying**, which is the part that
  silently breaks the live map: with a plain ProxyPass the upgrade never
  completes and the map loads correctly and then never moves. The `/ws/` rules
  must precede the catch-all or they never match.
- `deploy/courseops.service` - systemd unit binding 127.0.0.1 so the app cannot
  be reached directly and TLS bypassed, with filesystem hardening.
- `docs/DEPLOYMENT.md` - install, Apache, certbot, and a verification table
  covering each thing that fails independently, including a curl that proves the
  WebSocket returns 101 rather than assuming it.
- 2 tests (257 total) pinning the cookie behaviour on plain HTTP and behind a
  proxy claiming HTTPS.

#### Note
`serve --behind-proxy` warns if bound to anything but loopback, since that
leaves a route around TLS.

HTTPS also closes the geolocation gap from the known-gaps list: browsers block
the Geolocation API outside a secure context, so the field roles' "where am I"
dot only works once this is deployed properly.

Still open and recorded in `docs/DEPLOYMENT.md`: OpenStreetMap's tile policy
does not cover a service hosted for many clubs, per-organization backups, and
static asset caching after an update.

### 2026-09-02 - Documentation caught up with the setup UI

An audit before clearing session context found the docs describing the previous
shape of the project: `docs/PLAN.md` had no mention of the setup application or
organizations at all, and both `README.md` and `docs/RUNBOOK.md` still told a
club to configure everything from a terminal.

#### Changed
- `docs/PLAN.md` gains a **Setup application and tenancy** section: why the
  CLI-only setup contradicted the project's premise, the organization boundary
  and the rule that `may_access_event` checks the club before any per-event
  assignment, why admins get accounts while volunteers keep links, and the note
  that the course review finally became the screen Phase 2 asked for.
- `README.md` and `docs/RUNBOOK.md` lead with the browser now, keeping the CLI
  equivalents in collapsed sections since it is still quicker for a repeat
  setup.
- `CLAUDE.md` status names the setup application and states that only `.env` and
  `courseops serve` stay in a terminal.

### 2026-09-02 - Setup UI, admin accounts and organizations

The project's premise is that a club can stand this up without much effort, and
a CLI-only setup contradicted that: a dozen terminal commands before anyone saw
a map. Setup now lives in the browser.

Only two things stay outside it, because they happen before it exists: the
callsign in `.env`, and starting the server.

#### Added
- `/setup` - a signed-in setup application covering organizations, events,
  course import, aid stations, roster, access links and administrators.
- **Visual course review.** Phase 2's plan called for a review *screen*; it had
  been built as a CLI listing. Staged features are now drawn on a map, and
  selecting on the map and in the list stay in sync - which is the point,
  because organizer files are wrong in ways a list of names cannot show.
- **Organizations**, the tenancy layer, so this can be hosted for several clubs.
  Every event belongs to one; a club cannot see another's events or admins.
- Three admin roles: `system_admin` (the host), `org_admin` (a club officer,
  scoped to their club) and `event_admin` (specific events within their club).
- `admin.py`, `users.py`, `setup.html/js/css`, and 27 setup endpoints.
- First-run flow: with no accounts, `/setup` offers to create the first system
  administrator and closes permanently once one exists.
- 33 tests (255 total).

#### Security notes
- Every event-scoped route goes through one `may_access_event`, so widening or
  narrowing access is a change in one place.
- An event assignment left behind after someone changes club grants nothing:
  the organization check runs first. There is a test for exactly that.
- An org admin cannot create a system administrator, nor manage another club's
  people. The last active system administrator cannot be deleted or disabled.
- Session cookies are HttpOnly, SameSite=Lax, and Secure when served over HTTPS.

#### Fixed
- The setup page templated `__FIRST_RUN__`, which also matched the JavaScript
  variable of the same name - so the substitution produced `window.false =
  false` and the flag was silently never set. Found by inspecting the served
  HTML rather than trusting the code; the token is now `{{FIRST_RUN}}`.

### 2026-09-02 - Layer toggles are switches

#### Changed
- Course and map-layer checkboxes are now switches: green for on, red for off.
- Still a real `<input type="checkbox">` underneath, styled with `appearance:
  none`, so keyboard operation, screen readers and the label association keep
  working. Only the painting changed.
- Added a visible focus ring, since a switch has no other affordance for
  keyboard users, and a `prefers-reduced-motion` guard on the knob transition.

#### Note on the colours
Red/green is the worst possible pair for the commonest colour-vision
deficiency, which affects roughly one man in twelve - and this is read outdoors
where colour washes out anyway. The knob **position** is therefore the primary
signal and the colour only reinforces it. Do not remove the position change and
leave colour as the only cue.

### 2026-09-02 - SSID mismatches surface themselves in the UI

`courseops check-in` only helps if someone remembers to run it, and a check that
must be remembered will be forgotten - especially on race morning. The app is
already ingesting, so it already knows which SSIDs are transmitting. It now says
so unprompted.

#### Added
- A **Needs attention** section at the top of the panel listing any callsign
  transmitting on an SSID the roster does not name, with the packet count and
  what the APRS symbol says it is.
- One-click resolution: **"This is Aid 3"** repoints the roster entry at the SSID
  actually in use, keeping the label, category and assignment; **Ignore**
  dismisses a digipeater or igate. The symbol drives the wording, so equipment
  reads "Ignore (equipment)".
- A count badge on the collapsed sheet button, so a phone shows there is
  something to resolve without opening the panel.
- `POST /ssid/adopt` and `POST /ssid/ignore`, both behind `require_write()`.
  Read-only roles see the alerts but cannot act on them.
- A `resync` broadcast: adopting rewrites the roster, which is more than an
  incremental message can express, so clients reload rather than risk a
  half-updated state.
- 8 tests (222 total).

#### Fixed
- **Ignoring an SSID now hides positions already stored, not just future ones.**
  Found in the browser: after dismissing the digipeater it was still sitting in
  the station list, because exclusion only gated ingest. "Ignore" has to mean
  "off the map", which is what the word promises.
- Adopting across different callsigns is refused, guarding a mis-click that
  would silently reassign someone else's identity.

### 2026-09-02 - Wildcard SSID matching, exclusions, and pre-event check-in

Prompted by a real case: a volunteer signed up as `WX0MIK-1` but actually
beacons `WX0MIK-5`, and the same callsign runs a digipeater on `-7`.

#### Changed
- **The APRS-IS filter now asks for every SSID of each rostered callsign**
  (`b/WX0MIK*`), not the exact SSID. A wrong SSID at signup previously made
  someone silently invisible on race morning - no error, just an empty row - and
  a missing person is far worse than an extra marker. `build_filter(...,
  wildcard=False)` restores exact matching for a noisy callsign.
- Ingest keeps a packet whose **base callsign** is rostered, so `WX0MIK-5` is
  tracked even though the roster names `-1`. Without this the wildcard would
  have been pointless: the packet would arrive and then be discarded.
  Unexpected SSIDs are reported in the ingest summary rather than silently
  absorbed, since they are almost always a signup typo worth correcting.

#### Added
- `station_exclusion` and `courseops ignore` / `ignored`: dismiss an SSID by
  name once, before the event. This is the cost of the wildcard default - the
  operator's own digipeater or igate arrives too - and this is how it is paid.
- `courseops check-in <event>`: listen wide for a few minutes and report every
  SSID heard under each rostered callsign, flagging wrong-SSID roster entries
  with the command to fix them. Run it at a club meeting a week out; it turns a
  silent race-morning failure into a checklist item.
- `symbols.py`: APRS symbol interpretation, used by check-in to tell a
  digipeater or igate from a person. The symbol is the most reliable
  machine-readable clue to what a station actually is.
- `courseops remove-station`.
- 6 tests (214 total).

#### Note
Two filter tests asserted the old exact-SSID default and were rewritten. They
now state the reasoning, so the default is not quietly reverted later.

### 2026-09-02 - Operational status history

#### Added
- `roster_status_log`: every station status change, appended and never
  overwritten, with the transition and who made it.
- `GET /api/{event}/{token}/station-log` (optionally `?station_key=`), readable
  by every role.
- 4 tests (208 total).

#### Why now rather than with replay
`roster.op_status` holds only the current value, so the sequence was being
thrown away on every change - and it **cannot be reconstructed afterwards**. An
event run without this loses its status timeline permanently, so the table had
to exist before the first real event rather than when replay is built.

The immediate payoff is shift handover, not replay: "Aid 4 closed at 11:32,
reopened at 11:40 by AB" is what an incoming NCS operator needs, and the roster
row alone could never express a reopening. Replay (issue #2) is backlogged.

### 2026-09-02 - Lead runner tracking

The counterpart to the sweep. The sweep says when an aid station may close; the
leader says when it has to be ready. Between them they bracket the field.

#### Added
- `leaders.py` and the `lead_sighting` table. First male and first female
  tracked per course, from reports called in as runners pass aid stations.
- Bib colour per race (`course.bib_color`, `bib_color_name`), pre-set before the
  event and defaulting to the course line colour. It is a separate field because
  the two answer different questions: the line colour is a map choice, the bib
  colour is how an operator identifies a runner - "first yellow male just went
  through" is what actually gets said on the net.
- Derived pace and an estimate for the next aid station, computed from the last
  leg so it stays responsive to a runner slowing late in the race.
- Lead runner panel: bib colour swatch, where each leader was last seen, pace,
  next station with an estimate, a bib field, a "Passed <station>" button, a
  picker for corrections and an undo.
- `courseops bib-color` to pre-set colours before race day.
- 22 tests (204 total).

#### Deliberate choices
- **Sightings are stored; everything else is derived.** There is no tracker on
  the front runner - we only learn this when someone reports it on the net - so
  current position, pace and ETA all come from the sighting log and nothing can
  disagree with the reports the net actually made.
- **Undo exists** because mis-taps happen while NCS is holding a microphone.
- **Divisions are free text** in the database (`male`, `female` by default), so a
  club can add wheelchair or non-binary divisions without a migration.

#### Fixed
- **An implausible pace is now discarded rather than published.** Found in the
  browser: two sightings entered thirty seconds apart - exactly what happens
  when NCS catches up on backlogged reports - produced a 0:28/mile pace and an
  ETA of two minutes. An aid station told the leader is two minutes out when
  they are twenty would act on it. Paces outside 3:00-30:00 per mile are now
  treated as clock artifacts: the sighting is still recorded, but no pace and no
  estimate are shown.
- Palette colours are stored lowercase, matching `normalize_color()`, so a
  colour compares equal regardless of whether it came from the palette or a
  user.
- `Leader.division_label` is a property, so the object and its serialized form
  no longer disagree.

### 2026-09-02 - Phase 6: incidents

Runner pickups tracked by bib, with a status workflow. The first write that
creates something rather than flipping a flag.

#### Added
- `incidents.py` and the `incident` / `incident_log` tables. Status workflow
  `reported -> en_route -> picked_up -> closed`, with `status_at` reset on every
  change.
- Endpoints to open, restatus and edit an incident, plus a log readable by every
  role. All writes go through the existing `require_write()`, so the role gate
  did not need touching.
- Incidents in the state snapshot and broadcast live, each with its course
  position: "bib 1432, mile 9.1 of Full" is what gets said on the radio.
- Incident list and square bib-labelled map markers, coloured by status. NCS
  gets a pin-drop mode and inline bib/note fields; read-only roles see the same
  incidents without controls.
- 26 tests (179 total).

#### Deliberate choices
- **`status_at` is the age of the current status, not of the incident.** The
  thing NCS must see is "requested eight minutes ago and nobody dispatched", so
  the list sorts by status then by longest-waiting, and an unanswered report
  rises to the top on its own.
- **Dropping a pin does not ask for the bib.** A pickup is called in over the
  radio before anyone has read the number off the runner. The incident opens
  immediately with the bib blank and the field auto-focused, matching the model
  the schema documents. This also removed a `window.prompt`, which blocked
  automated testing and would have put a modal between NCS and the map at the
  worst moment.
- **Notes are capped at 200 characters and named "short note".** Bib, location,
  status and a brief operational note are enough to run the net; an open-ended
  field invites a medical narrative about an identifiable person, which would
  change our obligations and the organizer's. The cap is the guardrail, and
  there is a test asserting it.
- **Closed incidents leave the map but stay in the list**, so the map shows only
  live work while the record stays complete.
- Incident status uses its own colour scale, distinct from radio status, and
  square markers so they can never be confused with stations or aid stations.

#### Verified in a browser
- An untouched report showing 8m sorted above everything, in red.
- Dispatching it moved it below an incident that had been en route longer -
  longest-waiting-first working.
- Pin drop, auto-focused bib entry, save, and course position (16.2 mi of Full).
- Closing recorded `closed_at`, stamped the operator initials, removed the
  marker from the map and dropped the open count.

### 2026-09-02 - Aid stations ordered by course position, not by name

Aid station names were already free text - Greek letters, NATO phonetic, numbers
or place names all worked. The **ordering** did not.

#### Fixed
- Aid stations were listed alphabetically, which is wrong for every naming
  scheme a club actually uses:
  - Greek letters come out Alpha, Beta, **Delta, Epsilon, Gamma** - Gamma is
    third on the course but fifth by name.
  - `Aid 10` sorts before `Aid 2` as a string.
  - Place names ("Riverside", "Cemetery Hill") have no meaningful name order.
  A natural sort would fix only the numbers; a lookup table only the Greek.
  Ordering by **position along the course** is scheme-independent, and it is
  also the order NCS works in - aid stations close one after another behind the
  sweep.

#### Added
- `CourseIndex.order_along_course()`; places not near any course sink to the end
  rather than being dropped.
- Course position on every POI in the state snapshot, and in POI popups.
- `roster.poi_id` is now settable: `courseops post <event> <callsign> <poi_id>`
  posts an operator at an aid station. Most aid station operators never beacon,
  so the station they are posted at is their only source of position - and it
  lets the roster be read in course order too.
- `courseops courses` lists POIs in course order with their mile.
- Client sorts stations by course position before falling back to the label.
- 6 tests (153 total), including both mis-sorting cases as explicit regressions.

### 2026-09-02 - Phase 5: course-relative position

"Full-back at mile 14.2" - the number the net actually speaks, and the signal
that says a road segment is clear so aid stations can tear down and Logistics
can pull cones.

#### Added
- `geo.project_onto_line()` and `geo.cumulative_lengths()` - snap a point to the
  nearest place on a polyline, returning distance along, lateral offset and the
  snapped point. Distance along uses haversine so it agrees with
  `line_length_m`; the perpendicular projection uses local planar maths, which
  is accurate to well under a metre at these distances.
- `progress.py` with `CourseIndex` - course geometry prepared once and reused
  for every station. Cumulative lengths are the expensive part (1200+ haversines
  for a marathon), so they are not recomputed per packet.
- Course position on every position in the state snapshot and on every live
  WebSocket update: distance along, remaining, fraction, offset and course name.
- Station rows show the mile figure in place of the callsign when one is
  available; the popup adds "mile 14.2 of Full", remaining distance, and how far
  off the line the station is when that exceeds 60 m.
- `units.format_mile()`.
- 13 tests (147 total), including every-vertex checks against the real Mankato
  course.

#### Deliberate choices
- **A station further than 250 m from any course gets no mile figure**, rather
  than a plausible wrong one - someone acts on this number. The tolerance is
  generous on purpose: GPS is good to tens of metres, but the course line runs
  down the middle of the road while a sweep is on the shoulder, and a hand-drawn
  course can cut a corner by hundreds of metres. Worth revisiting against a
  GIS-produced course.
- **The course name always travels with the mile.** Where routes share road the
  station snaps to whichever line is nearest, which is a coin flip on shared
  pavement, so "mile 14.2" alone would be misleading.
- **One decimal place.** The geometry is not accurate to better than that, and
  more digits would imply precision we do not have.
- **The projection clamps at both ends**, so a station past the finish reports
  the finish rather than an extrapolation beyond it.
- Course geometry is loaded once per ingest task, not per packet. Re-importing a
  course mid-event needs a server restart; courses are set up beforehand.

#### Verified against the real course
- Stations placed at genuine mile 6.0 / 14.2 / 22.5 on the Mankato route report
  6.03 / 14.21 / 22.52 at 0.0 m offset.
- Sampled vertices across the whole route snap to under 1 m offset and agree
  with the precomputed cumulative distance to within a metre.

### 2026-09-02 - Full icon set and home screen install

#### Added
- Nine icon files generated by `tools/make_icons.py`: SVG, ICO, 16/32/48 PNG
  favicons, a 180px `apple-touch-icon`, 192/512 manifest icons, and 192/512
  **maskable** variants.
- Per-role web manifest at `/api/{event}/{token}/manifest.webmanifest`.
- iOS standalone metas so a home screen launch opens without browser chrome.
- Home screen install instructions in `docs/RUNBOOK.md`, including what to do
  when a phone is lost.
- 6 tests (134 total) covering icon availability and manifest correctness.

#### Why one SVG was not enough
- **iOS ignores SVG for home screen icons, and ignores the manifest too.**
  Without `apple-touch-icon.png`, adding to the home screen yields a blurry
  screenshot of the page.
- **Android crops maskable icons** to a launcher-chosen shape, guaranteeing only
  the central 80%. The maskable variants draw the mark smaller on purpose;
  verified by circle-cropping both variants side by side.
- **Neither platform wants our corner radius** - both apply their own mask, so
  full-bleed sources have square corners.

#### Notable
- The manifest is generated per role rather than served statically, because the
  app has no tokenless entry point and a fixed `start_url` would install a
  shortcut to a 404. `short_name` is the role, since home screen labels truncate
  and the role is the useful half when someone holds two links.
- Installing saves the bearer token to the phone's home screen. Consistent with
  the link model, but it makes a lost phone a link to revoke.

#### Changed
- Tightened the pulse geometry in every mark: at the first amplitude it touched
  the disc rim on all four sides and read as a bolt rather than a trace.

### 2026-09-02 - Course Ops branding

Applied the club's design brief. Recorded verbatim in `docs/DESIGN.md` alongside
the implementation decisions.

#### Added
- `docs/DESIGN.md` - the brief (naming, tagline, logo concepts, palette) plus
  how it was implemented and where it conflicted with field constraints.
- `logo-pin.svg` - primary mark: checkpoint pin whose interior is an
  oscilloscope trace. Used in the top bar and the desktop panel header.
- `favicon.svg` - compact mark, plus `theme-color` for mobile browser chrome.
- Navy/orange tokens in `app.css`: `--navy-900/700/500`, `--orange`,
  `--orange-ink`.
- Tagline "Track the course, run the net." in the desktop panel header.

#### Changed
- Top bar and panel header are navy with a safety-orange rule. **The map and the
  station panel are untouched** - navy owns the chrome only.
- Connection badge colours darkened for contrast against navy.

#### Design decisions worth keeping
- **Navy is chrome, not the map surface.** The field roles read this on a phone
  outdoors for six hours; a dark working surface loses contrast against glare,
  and the OSM tiles underneath are light regardless.
- **Safety orange never appears inside a station row.** Amber and red mean
  something specific there, and brand orange sits close enough to the stale
  amber to be misread as a warning. Status colour only ever appears on status.
- **`--orange-ink` exists because `#FF6A13` on white measures 2.87:1** - below
  the 4.5:1 needed for text. Safety orange is a surface and a mark, never small
  text on white. Contrast was measured, not eyeballed: white on navy 15.4:1,
  orange on navy 5.4:1, orange-ink on white 5.5:1.
- **The favicon is a pin, not the C.O. monogram the brief specified.** The
  monogram was built and tested: at 16px it is an unreadable smudge and reads as
  a wifi glyph. A 22px pin beat a 32px monogram side by side. The brief's own
  rule - use the monogram where the pin is too detailed - pointed the other way
  once measured. The monogram is still wanted for large-format use.

### 2026-09-02 - Renamed to Course Ops

`AprsWebTracker` was a working title. Renamed now rather than later: the repo is
private and single-author, so this is the cheapest it will ever be.

#### Changed
- Repository `AprsWebTracker` -> `CourseOps` (GitHub redirects the old URL).
- Python package `aprswebtracker` -> `courseops`.
- CLI `awt` -> `courseops`.
- Product name in all documentation and the page title.
- The SQLite database is unaffected; existing event data keeps working.

#### Note
- "Ops" in the product name has nothing to do with the Logistics team. The roles
  remain NCS / Liaison / Logistics, and this is called out in `docs/PLAN.md`
  because it is an easy confusion to make later.
- A colour specification is coming from the club. `docs/PLAN.md` gains a Visual
  design section recording where colour lives and the two constraints that must
  survive any palette: daylight legibility, and status never relying on hue
  alone.

### 2026-09-02 - Phase 4: NCS panel and operational status

The app's first write path, and the second of the two status axes.

#### Added
- Operational status per roster entry (`pending` / `active` / `closed`), set by
  NCS and **kept strictly separate from radio status**. "Aid 1, no APRS, On
  station" is a healthy row; "SAG 1, silent 16 min, Not started" is an alarm. A
  single merged badge could not say both.
- Category-specific wording for the same three states: an aid station is
  "Not staffed / On station / Torn down", a sweep "Not started / Rolling /
  Finished". Reading the wrong word on a radio net costs a clarifying exchange.
- `POST /api/{slug}/{token}/station/{key}/status` - the first mutation endpoint.
  It goes through `require_write()`, which is the single place role permission is
  enforced, so granting a field role write access later is a `WRITE_ROLES` change
  rather than an endpoint rewrite.
- Status changes broadcast over the WebSocket, so the read-only roles see NCS's
  changes immediately without reloading.
- `roster.op_status_at` / `op_status_by` columns (with migrations) and an
  operator-initials field, shown only to roles that can write. Typed once per
  shift, kept in the browser, stamped on each change so a handover can see who
  did what. Explicitly not authentication.
- Station rows sort by operational status first: closed sinks, because a
  torn-down aid station going silent is not news and would otherwise bury the
  rows that matter.
- 9 tests (128 total) covering the write path, role enforcement and broadcast.

#### Verified in a browser
- NCS sees three status buttons per station with category-correct wording;
  Liaison sees a read-only label.
- A **forged** POST from the Liaison view returns 403, so enforcement is
  server-side rather than hidden UI.
- Marking a sweep Finished moved it below a stale-but-pending SAG, confirming
  the sort.
- Initials and timestamp are recorded: `op_status_by: "MW"`.

#### Note
- Browsers cached `index.html` across an edit during testing. Harmless in
  development, but a club updating the app may need a hard refresh; worth
  revisiting with cache headers in Phase 8.

### 2026-09-02 - Operator runbook and documentation discipline

#### Added
- `docs/RUNBOOK.md` - event-day operating procedure for the club, not for
  developers: what to do one to two weeks out (get files, import, review, colours,
  What3Words, roster), race morning (start, verify, distribute links), during the
  event (what NCS watches, what the field roles see, common situations, revoking
  a leaked link), and afterwards. Includes a known-limitations list to set
  expectations, since each item otherwise reads as a bug: the HTTPS requirement
  for location, invisibility without cell coverage, non-continuous updates, and
  receive-only operation. Sections needing the club's own practice are marked
  **[CLUB]**; features not yet built are marked **[PHASE n]** rather than
  described as if they work.
- A **Documentation discipline** section in `CLAUDE.md`: a per-file table of what
  to update on every change, and the rules that keep it honest - a discovered
  constraint is documentation even with no code change; record why rather than
  what; a real-data finding becomes a test rather than a note; CLAUDE.md stays an
  index and never holds content.

#### Why
Documentation batched to the end gets skipped, and the reasoning behind a
decision is unrecoverable a week later. Making the checklist part of the repo
means it survives a cleared session rather than depending on being asked.

### 2026-09-02 - Session continuity pass

Captured what existed only in conversation, so a fresh session loses nothing.

#### Added
- `tests/fixtures/mankato_marathon.kml` - the real MapMyRun export of the 2026
  Mankato Marathon, plus `test_real_course.py` (8 tests). It is the only
  realistic course available and is the baseline for Phase 5. Synthetic fixtures
  did not reproduce what this file does: it has already caught identically-named
  placemarks separated only by `<styleUrl>`, and hint patterns that could not
  match inside `start_marker`. Tests assert the measured distance, the 157
  duplicate vertices, the 13 straight-line gaps, and that the file carries no
  aid stations - so a future export that differs is noticed rather than assumed.
- `docs/PLAN.md` gains a **Known gaps and open threads** section: GPX import
  (issue #1), the HTTPS requirement for geolocation, OpenStreetMap tile policy
  for hosted use, corner-cutting in hand-drawn courses and its effect on Phase 5
  mile figures, the absence of aid stations in course files, GPX point density,
  and the missing operator runbook.
- `docs/PLAN.md` resolved-questions list extended with the decisions made since
  it was written: the Liaison/Logistics split, multiple NCS operators sharing a
  workstation, draw order for overlapping courses, mobile-first, and no W3W API.
- `CLAUDE.md` now points a fresh session at `docs/PLAN.md` first and notes that
  open work is also tracked as GitHub issues.

#### Note
- The Mankato fixture is the organizer's course data from a publicly shared
  MapMyRun route. Flagged in the test module: decide before making the
  repository public whether to keep it, synthesize a replacement, or ask.

### 2026-09-02 - Logistics is its own role

Terminology correction from the field: what earlier entries called "Ops" is the
**Logistics** team, and it is a different group from the Liaison - Liaison is
embedded with Public Safety and Medics, Logistics is out on the course handling
traffic control, cone placement and teardown.

#### Changed
- `logistics` added as a third role with its own access link, so one field team's
  link can be revoked without cutting off the other. Both field roles are
  read-only; write access is now a `WRITE_ROLES` tuple rather than a hardcoded
  comparison, keeping the "grant it later" path a one-line change.
- Layer defaults apply to both field roles: aid station operators and net control
  hidden, everything else on. Sweeps stay on deliberately - the sweep marks the
  back of the pack, which is what tells Logistics a road segment is clear and the
  cones can come up.
- The `liaison` role key and its existing links are unchanged.

### 2026-09-02 - Location tracking for the viewer

#### Changed
- The locate button now TRACKS the viewer with `watchPosition` instead of
  taking a single `getCurrentPosition` fix. Ops and Shadow are defined by
  moving around the course; a dot frozen where they tapped five minutes ago is
  worse than no dot, because it still looks current. A second tap turns it off
  and removes the dot.
- Added an accuracy circle around the dot, and a warning when the fix is worse
  than 100 m - a 500 m "fix" is wifi triangulation, not GPS, and should not be
  trusted as a position.

#### Added
- A dedicated location status line. Location problems previously overwrote the
  connection badge, which would have masked whether the data feed was live.
- Distinct messages for permission denied, no fix yet, and no location support,
  instead of one catch-all.
- An explicit check for a non-secure context. Browsers block geolocation outside
  HTTPS (localhost excepted), so a club serving this over plain http:// on a LAN
  would otherwise see a bare permission error that looks like the user's fault.

#### Fixed
- Removed a leftover branch that panned to a station marker while "following",
  which conflicted with following the viewer's own position.

### 2026-09-02 - Phase 3: live map

#### Added
- `web.py` - FastAPI server. Role token in the URL path gates every route; an
  invalid token returns 404 rather than 403, so it cannot confirm that an event
  exists. Endpoints: the map page, a full state snapshot, and a WebSocket feed.
- `access.py` - role tokens (`ncs`, `liaison`). No user accounts: one long
  random URL per role, pasted into the right group text. Tokens are scoped to
  their event and can be revoked. NCS writes, Liaison is read-only.
- `hub.py` - in-process per-event fan-out. One APRS-IS connection feeds every
  browser. Subscribers have bounded queues: a stalled client drops messages
  rather than stalling the ingest loop, which is safe because clients resync
  full state on reconnect.
- `static/` - the map client. Leaflet, no build step. Courses drawn in
  `sort_order`, POIs with What3Words in popups, live station markers.
- Layer toggles with role defaults - Liaison starts with aid station operators
  hidden, keeping a phone screen readable. Preferences persist per browser.
- Station list sorted with whatever needs attention first: silent, then stale,
  then fresh, with non-beaconing operators last.
- Browser geolocation for "where am I" - local only, never transmitted.
- CLI: `serve`, `links`, `list-links`, `revoke-link`.
- Schema: `access_token` table.
- 21 tests (108 total) covering access control, state shape, and fan-out.

#### Fixed
- The connection badge was hidden behind Leaflet's zoom control, which sits in
  the same corner at a higher z-index. Found by rendering the page in a real
  browser; the top bar now reserves space for it. This mattered because the
  badge is the only signal that a phone is showing stale data.
- `Subscription` was unhashable: `@dataclass` generates `__eq__`, which unsets
  `__hash__`, and subscriptions live in a set. Value equality would also have
  collapsed two browsers on the same event into one subscriber.
- Replaced deprecated FastAPI `on_event` handlers with a lifespan context.

#### Client rules worth keeping
- Marker positions are never interpolated. Reports arrive every 1-5 minutes;
  animating between them would show a position that was never reported.
- Ages redraw on a timer so "2m ago" does not sit there reading 2m forever.
- Reconnect resyncs full state rather than replaying messages.

#### Verified in a browser
- The real 26.4 mi Mankato course renders over OSM tiles with start and finish
  markers, correct colours, and working layer toggles.
- Station shapes and status colours: a fresh sweep (green square) and a stale
  SAG (amber diamond) are distinguishable by shape as well as colour.
- The bottom sheet slides over a full-bleed map. A true 375px render could not
  be checked - window resizing was unavailable - so the narrow layout is
  verified by forcing the mobile stylesheet, not by a real phone viewport.

### 2026-09-02 - styleUrl disambiguation (found with real data)

Validated the importer against a real MapMyRun export of the 2026 Mankato
Marathon course: 1415 points, 26.40 mi measured against an official 26.22.

#### Added
- `KmlFeature.style_id` and an `import_feature.style_id` column, capturing
  `<styleUrl>`. Exporters routinely give several placemarks the SAME name and
  distinguish them only by style - MapMyRun names the start marker, the finish
  marker and the route itself all after the route. Without this the start and
  finish were indistinguishable in the review list, both falling back to
  `unassigned`.
- `suggest()` now considers `style_id`, correctly proposing `poi:start` and
  `poi:finish` for those markers.
- `review` prints the style whenever a name is not unique in the listing.
- 5 tests covering the MapMyRun shape (87 total).

#### Fixed
- Hint patterns never matched inside `start_marker`: `_` is a word character, so
  a ``-anchored pattern cannot match `start` there. Separators are now
  normalized to spaces before matching.

#### Verified against real data
- Dedupe removed 157 of 1415 points - MapMyRun repeats the vertex at every
  routing-segment join, which is what `dedupe_consecutive` exists for.
- 13 segment gaps over 200 m (largest 1241 m) where the route builder used
  direct/offroad mode instead of snapping to roads. The line therefore cuts
  corners, which matters for Phase 5 mile computation.
- Geometry extracted from the MapMyRun HTML page is byte-identical to the
  official KML export, so either source is usable.
- The export contains no aid stations - only the route, a start and a finish.
  Aid station locations must come from elsewhere regardless of file format.

### 2026-09-02 - Course styling and draw order

#### Added
- `styling.py` - course color and line-style handling. Colors come from the
  Okabe-Ito colorblind-safe palette minus the yellow, which vanishes against
  light map tiles; a new course takes the next unused one automatically.
- Adjustable draw order per course (`course.sort_order`). Where the Full, Half
  and 10K share road their lines are coincident, and draw order decides which
  one is visible. This is the primary control for overlap and will be adjustable
  in the UI.
- `course.dash_pattern` - opt-in line styles (solid, long, dotted, dash-dot,
  medium, or a raw SVG dasharray). Courses are solid by default; a dash is for
  the case draw order cannot cover, seeing two coincident routes at once.
- CLI `style-course` to change a course's color, line style, name or draw order.
  `assign-course` gained `--dash`, and `courses` now lists draw order and style.
- A column migration step in `init_schema`, applied only where missing.
- 17 new tests (82 total).

#### Fixed
- New columns would never have reached an existing database: `CREATE TABLE IF
  NOT EXISTS` silently skips a table that already exists, so `poi.what3words`
  and `course.dash_pattern` were unreachable on any file created earlier.
  `init_schema` now adds missing columns and reports what it applied.

### 2026-09-02 - Phase 2: KML/KMZ import

#### Added
- MIT license.
- `geo.py` - haversine distance, polyline length, consecutive-point dedupe, and
  segment stitching. Coordinates are (lon, lat) throughout, matching GeoJSON/KML.
- `kml.py` - KML/KMZ parsing built around the defects organizer files actually
  have: namespace variation, deep Document/Folder nesting, MultiGeometry,
  meaningless placemark names, coordinates with newlines and altitudes. Folder
  path is retained because it is often the only clue to what a feature is.
  Classification is advisory only.
- `importer.py` - two-phase import. Files stage into `import_feature` as
  `pending`; a human assigns each to a course or POI. Additive across files.
- Schema: `import_batch` and `import_feature` staging tables.
- CLI: `import`, `review`, `assign-course`, `assign-poi`, `discard`, `courses`,
  `set-w3w`.
- `tests/fixtures/messy_course.kml` - fixture reproducing real-world defects:
  a course split across segments with one drawn backwards, an "Untitled Path",
  a folder mixing aid stations with parking, a MultiGeometry, and a point with
  no altitude.
- 41 new tests (65 total), including hardening tests for entity expansion, XXE
  and zip bombs.

#### Changed
- Added `defusedxml` as a second runtime dependency. KML arrives from race
  organizers and will be uploaded through the web UI, making it untrusted
  third-party input; the stdlib XML parser does not guard entity expansion.
- KMZ archives are checked for decompression bombs and size-capped, which
  defusedxml does not cover since it only protects the XML parse.
- CLI output is ASCII-only. Em dashes rendered as mojibake in the Windows
  console, and a club laptop is the target environment.

#### Fixed
- `geo.stitch` grew the chain only from the tail, so a file listing a middle
  segment first got the front piece reversed onto the back, folding the course
  over itself. It now grows at both ends. This was a silent, plausible-looking
  corruption of a real course.

### 2026-09-02 — Phase 1: APRS-IS ingest

#### Added
- Project scaffold: `pyproject.toml` (src layout, `awt` console script), `.gitignore`,
  `.env.example`, venv-based dev setup. Single runtime dependency: `aprslib`.
- `schema.sql` — full domain schema. Every table carries `event_id` so one database
  file can host multiple events without restructuring: `event`, `course`, `poi`,
  `roster`, `position`, `raw_packet`.
- `parser.py` — raw APRS text to `PositionReport`. Delegates decoding to `aprslib`,
  which covers uncompressed, base-91 compressed, Mic-E and NMEA encodings. Rejects
  non-position packets and Null Island (0,0) with a typed `Rejected` reason.
- `aprsis.py` — async APRS-IS client. One connection for the whole application,
  receive-only login (passcode `-1`), roster-derived buddy filter, 120s read timeout,
  exponential backoff with jitter capped at 300s. Login line is never logged.
- `db.py` — SQLite access layer. WAL mode, foreign keys on, autocommit.
- `ingest.py` — feed to parse to store, with an `on_position` callback that is the
  seam the Phase 3 WebSocket fan-out will plug into.
- `units.py` — metric storage, US customary presentation. Speed in mph, altitude in
  feet, course distance in miles.
- `what3words.py` — normalize, shape-validate and format W3W addresses. No API calls.
- `cli.py` — `courseops init-db | add-event | add-station | roster | ingest | tail`.
- Test suite: 24 tests over a fixture corpus of APRS packet encodings, unit
  conversion, filter construction and roster semantics. No network required.
- `docs/PLAN.md` — full project plan, phase detail, domain decisions and risks.
- What3Words field on `poi`, maintained by NCS, entered by hand.

#### Fixed
- Ingest was discarding packets from rostered operators marked `expects_aprs=0`.
  The APRS-IS filter should request only stations expected to beacon, but the
  membership check must accept the whole roster — an aid station operator who
  turns a tracker on mid-event is still one of ours. Filter construction and
  roster membership are now separate queries (`tracked_station_keys` vs
  `all_station_keys`).

#### Verified
- `aprslib` 0.7.2 parses uncompressed, compressed and Mic-E packets correctly on
  Python 3.13, and normalizes speed to km/h and altitude to meters.
- End-to-end: event creation, roster with a non-beaconing aid station, filter
  generation excluding it, fixture packets through the real ingest handler, and
  `tail` rendering stored positions in mph/feet.
