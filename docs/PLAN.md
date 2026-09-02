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
| Packaging | Docker Compose + plain pip | One container, one volume |

Deliberately **not** used: Redis, Celery, Postgres, a JS build pipeline. None earn
their place at this scale, and each is one more thing for a club to install.

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
7. **Replay** — scrub the event afterward
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
- Browser geolocation for "where am I" — local only, never transmitted or stored
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
regardless of role, so granting a field role write access later is a permission
flag (`WRITE_ROLES`) rather than a rewrite.

## Key domain decisions

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
  one can be revoked alone. Both read-only in v1.
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

### Phase 7 - replay (backlogged, issue #2)

A time slider over a finished event. Backlogged deliberately: it is the only
phase that produces nothing on race day, and it is better designed against real
event data than an imagined one.

`position`, `raw_packet`, `incident_log`, `lead_sighting` and now
`roster_status_log` all keep full history, so an event is reconstructable
whether or not replay is ever built. A cheaper alternative worth weighing first
is a post-event CSV export rather than a scrubbing UI.

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

## Known gaps and open threads

Things discovered but not yet acted on. Each is a real constraint, not a wish.

- **GPX import** - GitHub issue #1. Consumer route tools (MapMyRun, Strava,
  Garmin) often export GPX only. Parser swap feeding the existing staging.
- ~~**Geolocation requires HTTPS.**~~ Resolved by Phase 8: Apache terminates
  TLS with a Let's Encrypt certificate, so the field roles' location dot works.
  A club running on a LAN without TLS still loses it.
- **OpenStreetMap tile policy** — issue #3. Fine for one club; not for a hosted
  multi-club service.
- **Per-organization backup and export** — issue #4. The current backup is
  whole-database.
- **Multi-tenant rough edges** — issue #5. Resource limits, a signup path with
  password reset, and static asset caching.
- **Hand-drawn courses cut corners.** The Mankato export has 13 straight-line
  gaps over 200 m (largest 1241 m) where the route builder used direct/offroad
  mode. Chords are shorter than the road, so Phase 5 mile figures drift on such
  files. GIS-sourced courses should not have this; `test_real_course.py` asserts
  the shape of the problem.
- **Course files carry no aid stations.** True of the MapMyRun export and likely
  of others, so aid station locations and their What3Words addresses are a
  manual step regardless of file format. A test asserts this so we notice if a
  future file does include them.
- **Point density.** A recorded GPX can carry thousands of points where a
  GIS-drawn KML carries dozens. Simplification is a separate decision; import
  must not silently discard fidelity.
- **The event time zone is stored but nothing reads it yet.** `event.timezone`
  is set in the UI and saved, but every timestamp is stored UTC and displayed as
  a relative age ("8 minutes ago"), which needs no zone. It becomes load-bearing
  the moment anything shows a clock time - a start time, a lead-runner ETA as
  wall clock, or Phase 7 replay scrubbing to "07:42". Capturing it now means
  those do not have to guess, and means an event is not silently assumed to be
  in the browser's zone - NCS may be running the net from another state.
- **Colour specification pending** from the club; see Visual design above.
- ~~**Static asset caching.**~~ Tracked in issue #5.
- **Operator runbook is a draft.** `docs/RUNBOOK.md` exists and covers what is
  built. Sections marked **[CLUB]** need the club's own practice filled in (who
  runs the pre-event check, how links are distributed, where backups go), and
  **[PHASE n]** sections fill in as those phases land.
