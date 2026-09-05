# Course Ops — Project Plan

## Problem

There is no simple web map that shows marathon course routes alongside live APRS
tracking of the ham radio operators supporting the event. Phone apps exist, and
full situational-awareness platforms like TAK exist, but nothing sits in between
that a radio club can stand up without significant effort.

## Goals

- One map showing all race courses (Full, Half, 10K), aid stations, and live ham positions
- Real-time position updates with no page refresh
- Standable-up by a club with minimal technical effort
- Possible future: hosted as a service for other clubs

## Non-goals (v1)

- Public/spectator access — access is restricted to event staff roles
- Transmitting on APRS — this application is receive-only, permanently
- Radio/TNC hardware interfacing — APRS-IS covers both phone apps and RF via igates
- Cross-event incident history

## Users and roles

| Role | Access | Notes |
|---|---|---|
| **NCS** (Net Control) | Full read + write | Multiple operators, typically sharing one workstation |
| **Liaison** | Read-only (v1) | Ham embedded with Public Safety / Medics; mobile phone use |
| **Logistics** | Read-only (v1) | Field team: traffic control, cone placement, teardown; mobile phone use |

Access control for v1 is a long random URL per role — a bearer token, exactly as
secure as the group text it is pasted into, which is appropriate for this data.
No user accounts. Deliberately chosen so a volunteer whose phone dies can be
re-admitted by re-sending a link, with no admin intervention.

## Scale

Under 50 hams per event. A flat roster list; no grouping or virtualization needed.
SQLite is nowhere near stressed.

## Technology

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python + FastAPI | `aprslib` handles all four APRS position encodings |
| Parsing | `aprslib` | Mic-E alone would be a major time sink to hand-roll |
| Storage | SQLite | One file, no server for a club to install; free replay |
| Frontend | Leaflet, no build step | No npm toolchain in deployment; mature touch support |
| Packaging | pip into a venv, systemd unit, Apache in front | No container runtime for a club to install |

Deliberately **not** used: Redis, Celery, Postgres, a JS build pipeline. None earn
their place at this scale, and each is one more thing for a club to install.

**Containers were planned and dropped.** This table said "Docker Compose + plain
pip" until Phase 8 actually shipped, which it did as a venv, a systemd unit and
Apache - and the row went unreconciled for a while, promising something that did
not exist.

The reasoning for dropping it: the deployment target is a club's ordinary VPS or
a laptop, where Docker is a runtime to install and a networking layer to debug
in front of an app that is one Python process and one SQLite file. It solves
nothing at that scale. It becomes attractive at the point of hosting several
clubs at once - see issue #5 - where reproducibility and isolation start paying
for themselves. Not before.

## Architecture

```
APRS-IS (rotate.aprs2.net:14580)
   |  ONE TCP connection, server-side buddy filter
   v
Ingest task (asyncio) -- parse --> SQLite
   |                                 |
   +------ in-process pub/sub -------+
                  |
                  v
        WebSocket /ws/event/{id} --> N browsers
```

One APRS-IS connection for the whole server, never one per browser. This is both
an efficiency matter and an APRS-IS etiquette matter — the network bans clients
that open many connections or reconnect in a tight loop.

## Build phases

1. **Ingest** — APRS-IS connect, parse, store. No UI. *(complete)*
2. **KML/KMZ import** — additive, with a review screen for messy organizer files *(complete)*
3. **Live map** — mobile-first, WebSocket updates, layer toggles *(complete)*
4. **Roster / NCS panel** — dual status axes, staleness scoped to `expects_aprs` *(complete)*
5. **Course-relative position** — "Full-back at mile 14.2" *(complete)*
6. **Incidents** — pin, bib, status workflow, operator initials, role-gated writes *(complete)*
7. ~~**Replay**~~ — dropped 2026-09-05; the report page answers the after-event questions
8. **Deployment** — Apache reverse proxy, Let's Encrypt, systemd *(complete;
   see `docs/DEPLOYMENT.md`)*

## Phase detail

### Phase 2 — KML/KMZ import

Organizers supply multiple files (full course, half course, water stops), typically
exported from different tools with inconsistent structure. Import is therefore
**repeatable and additive**: upload a file, unpack (KMZ is a zip containing
`doc.kml`), extract every Placemark, then present a **review screen** where each
LineString is assigned to a course and each Point to a POI type.

Build the review screen despite it feeling like scope. Organizer KML reliably
contains placemarks named "Untitled Path", routes split into several segments,
and folders mixing water stops with parking. Auto-detection will be wrong often
enough that a silent importer costs more time than the review screen does.

Also needed before mile markers mean anything: stitching multi-segment routes into
one line, and reversing a line drawn finish-to-start.

### Phase 3 — Live map (mobile-first)

The Liaison, Logistics and Shadow roles are the primary mobile case: outdoors,
one-handed, on a phone that must survive a six-hour event.

- Map is full-bleed; panels are bottom sheets in thumb reach
- High-contrast light theme by default — thin grey-on-grey is invisible in sunlight
- Markers differ by **shape and size**, not hue alone
- Nothing hover-only, ever. Tap targets >= 44px
- **Never interpolate marker movement** between fixes. A position that was never
  reported is worse than a stale one, because someone will act on it. Discrete
  jumps are more honest *and* easier on the battery
- Visible reconnect state; full state resync on reconnect
- Browser geolocation for "where am I" — local only, never transmitted or
  stored. The one exception is deliberate and per-press: see "Reporting is
  everyone's job" under Phase 6
- Design at 375px first; the NCS desktop layout is the enhancement

### Phase 4 — Roster / NCS panel

**Two independent status axes. Do not merge them into one badge.**

*Operational status* — manual, NCS-set with one tap:

| State | Aid station | Sweep / SAG |
|---|---|---|
| `pending` | Not yet staffed | Not yet rolling |
| `active` | On station | Rolling |
| `closed` | Torn down | Finished |

*Radio status* — automatic, derived from the feed: `fresh` / `stale` / `silent`,
or `n/a` where `expects_aprs` is false.

So "Aid 4 — W1ABC — On station — no APRS" is a healthy row, while
"Full-back — K2XYZ — Rolling — silent 18 min" should be shouting. Collapsing these
loses the exact distinction that makes the panel worth watching.

Layer toggles, defaults by role:

| Layer | NCS | Liaison | Logistics |
|---|---|---|---|
| Courses, aid station locations, incidents | on | on | on |
| Sweeps, SAG | on | on | on |
| Aid station operators | on | **off** | **off** |

Sweeps stay on for both field roles, and for Logistics that is the whole point:
the sweep is the back of the pack, so its position is what says a road segment
is clear and the cones can come up. This is the same signal that lets NCS close
aid stations, which is why Phase 5 (course-relative position) serves both.

Toggles persist in the browser across reconnects and phone locks.

### Phase 4a — What3Words for aid stations

Each aid station POI carries an optional What3Words address. Aid stations sit at
road intersections and park entrances where a street address is useless and a
lat/lon is painful to read over voice; three words is the format that actually
survives a radio net.

- **Maintained by NCS** — entered and edited from the NCS view, read-only to the
  field roles, consistent with the existing role split.
- **Manual entry. No API integration, now or later.** What3Words is a paid
  service; we are not buying it. NCS looks the address up and types it in. This
  also keeps club setup free of a signup and a billing relationship for a field
  that changes once per event.
- Consequence: we cannot verify an address or resolve it to coordinates.
  Validation is shape-only (three dot-separated words) and advisory. The KML
  lat/lon stays authoritative; W3W is a convenience for voice traffic.
- Displayed prominently in the aid station popup with one-tap copy, so NCS can
  read it out or paste it into a text message.
- Store the words as given; validate only loosely (three dot-separated words).
  Do not attempt to resolve or verify offline.

### Phase 3a - Course styling and draw order

Each course carries a color, an optional dash pattern and a draw order, all
adjustable.

The Full, Half and 10K share road, so their lines are coincident for miles.
**Draw order is the control for that** (`course.sort_order`, higher draws on
top) and must be adjustable in the UI - the operator decides which route wins on
shared pavement. Courses are solid by default; nothing is imposed.

Dash patterns stay available as an opt-in for the one thing draw order cannot
do - showing two coincident routes at once, since a dashed line on top lets the
solid line beneath show through its gaps.

Default colors are Okabe-Ito, minus the yellow which vanishes on light map
tiles. Colorblind-safe matters more than usual on a course map.

### Phase 5 - Course-relative position

Project each station onto the nearest course line: "Full-back is at mile 14.2".
Far more actionable over a radio net than a lat/lon.

This is the operational loop of the whole event: once the full-back passes mile 6,
NCS closes Aid 1-5 and roads reopen. With most aid stations dark, the sweeps and
SAG are nearly the entire live picture, which makes this more valuable than its
position in the phase order suggests.

### Phase 6 — Incidents

Model as an **incident**, not a pin; the pin is just how it is drawn.

- Bib number (identity key; may be blank initially and filled in later)
- Location (map click, or "at Aid 4" by picking a POI)
- Status: `reported` -> `en route` -> `picked up` -> `closed`, with timestamps
- Short operational note; who reported, who is assigned, who closed
- Operator initials, stored in the browser, stamped on every mutation — not
  authentication, just a log annotation so a shift handoff can see who did what

Status transitions are the point. A pin that is only "there" or "gone" will not
survive contact with a real event; you need to see a pickup requested 8 minutes
ago with nobody dispatched. Sort by time-in-current-status.

**Keep medical detail out.** Bib, location, status and a short operational note
("unable to continue, waiting at mile 9") are fine. Inviting narrative descriptions
of a runner's condition would make this a system storing health information about
identifiable people, changing both our obligations and the organizer's. The bib is
the organizer's identifier — we will never have the bib-to-name mapping and should
not want it.

All pin mutations go through one server-side endpoint that records who and when,
regardless of role. Permission is per capability (`access.ROLE_CAPABILITIES`),
so widening a role is a change to that table rather than to the endpoints - which
is how SAG gained the pickup queue and nothing else.

#### Reporting is everyone's job; the queue is not (done)

Opening an incident and working one are different jobs, so they are different
capabilities. `CAP_INCIDENT_REPORT` creates a pickup or a course note and fills
in its bib, note and position; `CAP_INCIDENTS` moves one along its workflow and
deletes it. All four roles hold the first. Only NCS and SAG hold the second.

The reason for the split rather than simply granting `CAP_INCIDENTS` to
everyone: the pickup queue and its count are read as "who is still waiting", so
the ability to close or delete an entry is the ability to make that count lie.
A bearer link is only as good as the phone it is on, and Liaison's and
Logistics' links are handed out most widely. Adding to the board is safe;
silently emptying it is not.

The reason for granting anything at all: all four teams are out where things
happen. Requiring a Logistics volunteer at an intersection to relay a pickup
over the radio to whoever holds the NCS link means it arrives late, or garbled,
or not at all - and that is the failure the incident board exists to prevent.

**Two ways to place the pin, and the tap is the primary one.** Liaison sits at
the EOC with Public Safety and Medics, taking reports about places they have
never seen: for them their own location is the one position that is always
wrong. So "Here" is a second button beside "Drop a pin", never a replacement
and never the default.

**"Here" is the single exception to the geolocation privacy rule.** The locate
dot is local, always - never sent, never stored, invisible to other viewers.
Pressing "Here" sends one fix, once, as the location of an incident the person
is deliberately publishing. That is a per-press disclosure of a place rather
than continuous tracking of a person, and the distinction is the whole reason
it is a separate button rather than a background behaviour. If a fix comes back
worse than 100 m the pin is still created - a rough position with a note beats
no report - but the accuracy is stated on screen so nobody reads wifi
triangulation as GPS.

Known limitation: a pin placed at a bad fix cannot be dragged to correct it.
`update` accepts `lat`/`lon` and a reporting role may call it, but there is no
UI for moving a marker yet. Today the fix is for NCS or SAG to delete it.

## Key domain decisions

- **The area around the course is heard, and only matched stations are
  stored.** Decided 2026-09-05, on the first live feed. The buddy filter was
  chosen to avoid storing the public's location, and it also could not hear
  the person on the roster when they were using someone else's radio - which
  turned out to be common. The resolution keeps the privacy posture and fixes
  the operational one: the feed asks for the roster PLUS a radius (course
  extent plus a mile); what the roster does not know is held in memory, shown
  only to NCS, and stored only once NCS says who it is. "Buddy filter, not
  area filter" is superseded by "area filter, seen not stored".

- **Receive-only, passcode `-1`.** Grants read access and no transmit capability.
  The club needs a callsign and no secret at all.
- **Buddy filter, not area filter.** The roster is known in advance, so ask APRS-IS
  for exactly those stations. Orders of magnitude less traffic, and it avoids
  incidentally storing the location of the public.
- **The roster is not the APRS feed.** Three separate things: the *aid station*
  (a location from KML), the *operator assigned to it* (a callsign), and a
  *position report* (only if they beacon). Most aid station operators never beacon.
- **`expects_aprs` gates staleness alerting only.** If the "who has gone quiet"
  panel lists 30 operators who were never going to beacon, it is noise and will be
  ignored within twenty minutes. It does *not* mean "discard their packets".
- **SSID is part of station identity.** `N0CALL-9` and `N0CALL-7` are different
  radios on different people.
- **Symbol table and code travel as a pair.** The table character changes the
  meaning of the code.
- **Store metric, display US customary.** aprslib normalizes to km/h and meters;
  converting at ingest would make stored units depend on who ran the importer.
- **Everything is event-scoped from day one**, even with a single event. One
  SQLite file with `event_id` on every table. Retrofitting multi-tenancy is
  painful; designing for it now costs nothing.

## Known risks

- **Sparse, irregular updates.** Phone apps beacon every 1-5 minutes and rural
  courses have cell dead zones. "Last known, 4 minutes ago" is the *normal* state,
  not an error state.
- **Igate coverage is not ours to fix but is ours to explain.** A ham with no data
  vanishes from the map; make that legible rather than mysterious.
- **Privacy posture.** These positions are already public on APRS-IS, but a page
  following named volunteers reads differently. The buddy filter helps: we only
  store people who consented by being on the roster.
- **APRS-IS etiquette.** One connection, proper filter, app name and version in the
  login, exponential backoff with jitter on reconnect.

## Resolved questions

- *Does the Liaison need a subset of the station roster?* - Full set, with layer
  toggles defaulting aid station operators off.
- *Incidents across events?* - No. Event-scoped only.
- *Roster size?* - Under 50.
- *Public access?* - None beyond the staff roles.
- *Units?* - Store metric, display US customary.
- *Who maintains What3Words?* - NCS, by hand. No API; it is a paid service.
- *Liaison and Logistics: one role or two?* - Two. Different teams (Liaison with
  Public Safety/Medics, Logistics on traffic/cones/teardown), separate links so
  one can be revoked alone. Both read-only.
- *Does SAG need its own role?* - Yes, added later. Their question is "who am I
  going to collect", not "where is everyone", and they were reading a view built
  for road clearance. The only field role that writes, and only the pickup queue.
- *Multiple NCS operators?* - Yes, typically sharing one workstation. A shared
  role link covers it; operator initials (Phase 6) handle shift handover.
- *Overlapping courses?* - Solved by adjustable draw order, not forced dashes.
- *Mobile?* - Mobile-first. The field roles are the primary case.

## Naming

The product is called **Course Ops**. `AprsWebTracker` was the working title.

Renamed throughout on 2026-09-02: repository `CourseOps`, Python package
`courseops`, CLI `courseops`. Note that "Ops" here is the product name and has
nothing to do with the Logistics team; the roles are still NCS / Liaison /
Logistics.

## Visual design

A colour specification is coming from the club and will replace the current
placeholder palette. Two constraints must survive whatever it says, because both
came from how the app is actually used rather than from taste:

- **Daylight legibility.** The field roles read this on a phone outdoors. Pale or
  low-saturation values disappear on light map tiles.
- **Status must not rely on hue alone.** Radio status and station category are
  currently distinguished by shape and lightness as well as colour, so they
  survive a washed-out screen and colour-vision deficiency.

Where colour lives today, and what a spec would touch:

| Place | What it controls |
|---|---|
| `static/app.css` `:root` | UI tokens: ink, paper, lines, accent, and the four radio-status colours |
| `styling.py` `DEFAULT_COLORS` | Course line palette (currently Okabe-Ito minus yellow) |
| `static/app.css` `.stn--*`, `.poi-icon` | Marker fills and shapes |

### Lead runner tracking

First male and first female per race, reported as they pass aid stations. The
counterpart to the sweep: the sweep says when a station may close, the leader
says when it must be ready.

There is no tracker on the front runner, so this is a **log of reports** called
in over the net, not a track. Position, pace and the estimate for the next
station are all derived from that log, which keeps them from disagreeing with
what the net actually said.

Bib colour is pre-set per race before the event and defaults to the course line
colour, but is a separate field: the line colour is a map choice, while the bib
colour is how an operator identifies a runner in front of them.

Entered by NCS, not by aid stations directly. It needs to be quick - a
"Passed <next station>" button, a bib box and an undo - but not one-tap, since
NCS is at a workstation.

Pace outside 3:00-30:00 per mile is discarded as a clock artifact rather than
shown; reports arrive in bursts and a bad ETA is worse than none.

### Phase 7 - replay (dropped)

Dropped 2026-09-05, issue #2 closed. A time slider over a finished event was
the one phase that produced nothing on race day, and once the report page
existed the after-event questions the club actually has - how many pickups,
when, where, what went wrong at which corner - were answered without it.

Nothing was removed to drop it: `position`, `raw_packet`, `incident_log`,
`lead_sighting` and `roster_status_log` keep full history, so an event is still
reconstructable by hand from the database if a question ever needs it.

## Setup application and tenancy

Added after Phase 6, when it became clear the CLI-only setup contradicted the
project's own premise: a club was facing a dozen terminal commands before seeing
a map. `/setup` covers organizations, events, course import and review, aid
stations, roster, access links and administrators.

Only two things stay outside the browser, because they happen before it exists:

- the callsign in `.env`
- `courseops serve`

The CLI is kept, not replaced - it is better for repeat or scripted setup, and
it is how the test suite drives the same code paths.

### Organizations are the tenancy boundary

Added so this can be hosted for several clubs. Every event belongs to exactly
one organization, and a club sees nothing of another's events, admins or race
calendar.

```
system_admin (the host)
  +-- organization  (a club)
        +-- org_admin    runs the club's events, manages its people
        +-- event        -> event_admin, assigned events only
```

`users.may_access_event()` is the single place access is decided. It checks the
**organization first**, before any per-event assignment, so an assignment left
behind after someone changes club grants nothing. There is a test for exactly
that case.

Events created before organizations existed are adopted into a default
organization on upgrade, rather than being stranded and invisible.

### Accounts for admins, links for volunteers

Deliberately split. Volunteers on the day keep bearer role links: a link can be
re-sent to someone whose phone died, at 6am, by anyone holding it, with no
account recovery and no admin awake to do it. Setup is different work - it
happens beforehand, by a named person, and needs to be attributable, so it uses
accounts.

Passwords use scrypt from the standard library. Each hash carries its own salt
and cost parameters, so the cost can be raised later without invalidating
existing passwords. Sessions live in the database so they can actually be
revoked - on logout, password change, or deactivation.

### The course review became a screen

Phase 2's plan called for a review *screen*; it was first built as a CLI
listing, which was exactly the friction the plan was trying to avoid. Staged
features are now drawn on a map, with list and map selection in sync, because
organizer files are wrong in ways a list of names cannot reveal.

### Pin labels (done)

Pins are labelled with one or two characters derived from the place name, so a
glance answers "which one is that?".

Decisions taken, so they are not re-litigated:

- **Characters on the pin, not names beside it.** Names collide into a smear at
  the whole-course zoom, which is the zoom NCS actually sits at. Names are
  available as an opt-in switch, gated on zoom, and default off.
- **Derivation is a guess and is never stored.** Storing it would freeze the
  label when the place is renamed. `poi.label` is an override only, and is
  cleared when it agrees with the guess.
- **A number in the name beats a letter.** "Aid 1/2/3" is at least as common as
  "Alpha/Bravo/Charlie", and first-letter derivation labels the whole course
  "A". Words describing the kind of place ("aid", "water", "stop") and the race
  ("ALL", "FULL", "half") are skipped, because the real Mankato export is full
  of `WATER (ALL)`.
- **Two characters is the budget.** A marker is 24px; a labelled one is 30px.
  Three characters do not fit, and text that does not fit is worse than none.
- **A labelled pin loses its glyph.** They cannot both fit. This is why labels
  are per layer: in practice one layer is labelled and the rest keep glyphs.
- **Collisions are surfaced, not solved.** Six places named `WATER (ALL)` all
  derive `W`, because their names are genuinely identical. The Places table
  shows each label so a club can see that and rename or override. Inventing
  distinct labels would put characters on the map that match nothing anyone
  says on the radio.

### Ordering places (done)

Geometry orders places by distance along the nearest course. That works for one
route and breaks for more than one: where routes share pavement the snap is a
coin flip, so the list interleaves miles measured on different races.

- **The club's order wins.** `poi.sort_order`, set by dragging in setup.
- **0 means unplaced and sorts last**, by distance. An event nobody has ordered
  is unchanged, and a later import lands at the end rather than the middle.
- **Both the Places table and the lead runner progression use it**, or "the
  next station" would disagree with the list the club just arranged.

**Which race a stop serves is now stated, not guessed** (`poi_course`). A stop
serves any number of races, because one water stop routinely serves several -
the organizer's file says so, naming them "WATER (ALL)". A place with nothing
stated falls back to the proximity snap, which keeps existing events working;
that fallback picks exactly one race and is what made a progression skip stops.

### Live tracking is a switch, and off by default (done)

Turning the APRS-IS feed on was a systemd edit. It is now a switch in
`/setup` -> Tracking, because the person responsible for the net is not
necessarily the person with a shell on the server, and this is something
switched on the morning of an event and off again afterwards.

Decisions taken:

- **Off by default, for privacy rather than cost.** The buddy filter asks for
  each operator's callsign wherever they are, not only on the course. A feed
  left running records where volunteers live and work, which is the same
  concern that made this a buddy filter rather than an area filter.
- **Persisted on the event, not held in memory.** A deploy restarts the
  service; a feed that failed to come back mid-event looks exactly like a quiet
  net, and nobody goes looking.
- **Still exactly one connection.** Enabling one feed stops any other, and
  enabling a running one is a no-op, so a double press cannot double the
  connections.
- **Refuse rather than fail silently.** No usable callsign means the switch
  will not move, with the message from settings that says what to put in
  `.env`. A switch reading "on" with nothing behind it is worse than no switch.
- **The filter is shown.** An empty or wrong filter is the commonest silent
  failure here: the feed connects, nothing matches, and the map stays blank
  while everything reports healthy.

**Never exercised against real traffic.** Every run so far has been with the
feed off. This is the largest untested claim in the project.

## Known gaps and open threads

- **The what3words coordinate URL is undocumented.** Linking to a square by
  latitude and longitude - `what3words.com/?maptype=roadmap&zoom=19&lat=..&lng=..`
  - is what the Aid stations table uses to turn "look this up somehow" into one
  click. It works: the site rewrites the URL to the square it resolved, verified
  2026-09-03 against real course coordinates. But what3words documents only
  word-based links, and the `w3w://show` app URI scheme has no coordinate form
  at all, so nothing obliges them to keep it. If it breaks, the coordinates in
  the column beside the link are ours and still do the job; there is no reason
  to reach for the paid API over it.

Things discovered but not yet acted on. Each is a real constraint, not a wish.

- ~~**GPX import**~~ - done 2026-09-05, issue #1. `gpx.py` reads tracks,
  routes and waypoints into the same features KML produces; dispatch is on the
  root element, so the extension does not matter. Point density is warned
  about, never thinned - simplification stays a separate decision.
- ~~**Geolocation requires HTTPS.**~~ Resolved by Phase 8: Apache terminates
  TLS with a Let's Encrypt certificate, so the field roles' location dot works.
  A club running on a LAN without TLS still loses it.
- **OpenStreetMap tile policy** — issue #3. Fine for one club; not for a hosted
  multi-club service.
- **No way to archive a finished event off the live server** — issue #4,
  reframed 2026-09-05. The feed records where volunteers live and work, and
  after an event those positions sit in the live database indefinitely. The
  fix is a per-event archive - a SQLite file in the same schema holding one
  event, viewable by running the app against it - followed by a separate,
  confirmed delete. Nothing install-scoped (users, sessions, tokens) travels.
  The organization-level export and restore that #4 used to be is now part
  of #5, because it needs a second club to design against.
- **Multi-tenant rough edges** — issue #5. Resource limits, a signup path with
  password reset, and static asset caching.
- ~~**Hand-drawn courses cut corners.**~~ Decided 2026-09-05: accepted, not a
  gap. The Mankato export has 13 straight-line gaps over 200 m (largest
  1241 m) where the route builder used direct/offroad mode, so the line is
  shorter than the road and Phase 5 mile figures drift on such files. The
  club's position is that the course line exists to *see the route*; the
  authoritative distance information is the mile-marker places the organizer
  supplies, and those are exact regardless of how the line was drawn. So
  nothing will smooth, snap or re-measure a course, and a snapped mile figure
  is a convenience with the course name beside it, never the number someone
  plans around. `test_real_course.py` still asserts the shape of the file so
  a change in the organizer's export is noticed.
- **Course files carry no aid stations.** True of the MapMyRun export and likely
  of others, so aid station locations and their What3Words addresses are a
  manual step regardless of file format. A test asserts this so we notice if a
  future file does include them.
- **Point density.** A recorded GPX can carry thousands of points where a
  GIS-drawn KML carries dozens. Simplification is a separate decision; import
  must not silently discard fidelity.
- ~~**The event time zone is stored but nothing reads it yet.**~~ The report
  page (issue #7, 2026-09-05) reads it: times are formatted by the browser in
  that zone. Everything below still holds for anything server-rendered.
- **The event time zone was stored before anything read it.** `event.timezone`
  is set in the UI and saved, but every timestamp is stored UTC and displayed as
  a relative age ("8 minutes ago"), which needs no zone. It becomes load-bearing
  the moment anything shows a clock time - a start time, a lead-runner ETA as
  wall clock. Capturing it now means
  those do not have to guess, and means an event is not silently assumed to be
  in the browser's zone - NCS may be running the net from another state.
- **Binding picks one SSID and keeps it.** A bare-callsign roster entry binds
  to the first non-infrastructure SSID heard and does not re-bind on its own,
  because a marker that moves between two radios mid-event is worse than one
  that is merely on the wrong radio. Someone who genuinely switches rigs needs
  NCS to press "This is <label>" on the new SSID, which rebinds. There is no UI
  for unbinding without a new SSID to point at; `db.unbind_station` exists for
  when that turns out to be needed.
- **Non-ham volunteers cannot be tracked today** - issue #6. Bike medics, race
  staff and non-licensed drivers have no callsign, and APRS is the only tracking
  mechanism. The medic is often the person NCS most wants to locate.

  The finding that decides the design: **a web page cannot track a phone
  reliably in the background.** iOS suspends JavaScript when the tab is
  not foreground or the phone locks, and Android Chrome throttles it hard. So
  "open this link and we will track you" stops the moment the phone is pocketed,
  and stops *silently* - leaving a dot that looks live. That is the same failure
  this project already refuses elsewhere: a stale position someone will act on
  is worse than none. A dedicated app holding real background-location
  permission (OwnTracks, Traccar Client - both free and open source) posting to
  an ingest endpoint is the reliable route; a browser link is only reliable in a
  mounted, charged vehicle.

  Privacy posture differs from a ham's: an APRS beacon is already public and we
  only receive it, whereas a medic's phone position is private data we would be
  actively collecting. Needs explicit, time-bounded, event-scoped consent and
  deletion afterwards. Identity would be a synthetic roster key (`MEDIC-1`)
  rather than a callsign - sayable on the net, unlike a UUID.

  Both candidate apps take a custom endpoint (verified, see issue #6): Traccar
  Client sends OsmAnd-protocol HTTP GET parameters, OwnTracks POSTs JSON and can
  be provisioned by a QR code carrying its whole config.

  **Decided (parked, not built): one URL and one QR code for the whole event,
  and each person types their own designator** - `Medic 1`, `Medic 2` - into the
  app's device-identifier field. One code printed once on a card or a sign,
  prepared ahead of the event; nothing to collate and nothing to hand to the
  wrong person. The alternative, a QR per person, needs no typing and is
  separately revocable, but means fifteen squares that must reach the right
  hands - and giving Medic 2 the wrong one makes them Medic 1 on the map,
  silently.

  One code rather than fifteen is the point: distributing per-person codes is a
  sorting problem at 6am whose failure is silent - hand Medic 2 the wrong square
  and they are Medic 1 on the map, confidently.

  **OwnTracks is the default for the QR step.** Its
  `owntracks:///config?inline=<base64>` is a documented, supported provisioning
  mechanism. Traccar Client's equivalent deep link exists but is not a public
  contract - the maintainer's guidance is to generate it from the Traccar server
  web app, which is the server we are deliberately not running, and there are
  unresolved iOS reports against it. The cost is that OwnTracks' `tid` is
  conventionally two characters, so designators are `M1`, `B2` rather than
  `Medic 1`; short is better radio practice anyway, and the roster row still
  carries the full label. Traccar Client is still worth accepting as a second
  option - its OsmAnd GET parameters are the easiest thing to receive and its
  free-text *Device identifier* takes `Medic 1` verbatim - it just cannot be the
  one a club is told to scan.

  Two consequences of the shared token, both handled the way this app already
  handles the equivalent APRS problems: **the roster is the allowlist** (only
  known designators are stored, unknown ones surface in the UI like
  `ssid_alerts` rather than vanishing), and **designators must be normalised**
  for case and for `_`/`-` before matching, or `Medic1` and `Medic 1` are two
  people and one of them is invisible while transmitting happily. Revocation
  becomes all-or-nothing, which is an acceptable trade for a one-day event.

  **Offline buffering brings its own trap:** a buffered burst arriving ten minutes late is a set of
  real positions from ten minutes ago, so staleness must key off the reported
  timestamp and never arrival time - or a medic returning to coverage looks
  freshly located somewhere they left. Out-of-order and duplicate points come
  from the same mechanism.
- **Course notes have no export** - issue #7. Their whole value is the organizer
  reading them after the event, which currently means reading them off a screen
  before the database is cleared.
- ~~**Place types were a fixed list.**~~ Resolved: `poi_category` is per-event
  data with no limit, and `staffed` replaced the hardcoded `aid_station` check.
  The remaining constraint is display, not storage: past a handful of layers,
  icon-plus-colour stops being reliably distinguishable outdoors and with colour
  deficiency. That is a reason to choose defaults well, never a reason to cap
  the count - a club with a mile marker layer of 26 pins wants it switched off
  by default, which the `visible` flag covers.
- *Two teams, two alphabets: should the app disambiguate?* - **No.** Medical
  and the water stops are numbered independently, so the medical team's Juliet
  and the water stops' Juliet are different corners, and a bare "Juliet" on the
  net means two things. The app could prefix names with their layer
  automatically, but then the screen would say "Water Stops Juliet" while the
  radio says "Juliet" - inventing a second vocabulary. Decided instead that the
  club names the points explicitly ("Ham Alpha", "Medic Alpha"), so the map says
  exactly what people say. The place-rename table shows each point's layer while
  you type, which is what makes that practical.
- ~~**We have not seen a real organizer's points file yet.**~~ Received, and it
  answered the question in an unexpected way: the points arrive in one flat
  folder, but the exporter's `Type` attribute names the layer for every point,
  so they effectively arrive pre-sorted after all - just not as folders. Import
  now creates the layers and files each point. The bulk-move work is still
  needed as the fallback for files that carry no such attribute. The one real export we have (Mankato, MapMyRun) is entirely flat -
  no `<Folder>` elements - so places import into a single layer and are sorted
  afterwards in the Places table. If a club's file does turn out to use folders,
  seeding layers from the folder names would be a small, obvious improvement.
  Nothing depends on either shape; `test_real_course.py` pins what we know.
- **Colour specification pending** from the club; see Visual design above.
- ~~**Static asset caching.**~~ Tracked in issue #5.
- **Operator runbook is a draft.** `docs/RUNBOOK.md` exists and covers what is
  built. Sections marked **[CLUB]** need the club's own practice filled in (who
  runs the pre-event check, how links are distributed, where backups go), and
  **[PHASE n]** sections fill in as those phases land.
