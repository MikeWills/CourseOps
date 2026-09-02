# Course Ops

**Ham radio event tracking and communications**
*Track the course, run the net.*

A web map for marathon-style events: race courses and aid stations from the
organizer's KML/KMZ files, overlaid with live APRS positions of the ham radio
operators supporting the event.

There are phone apps, and there are full situational-awareness platforms like TAK.
This aims at the gap between them — something a radio club can stand up for a race
without much effort.

**Status: early development.** APRS-IS ingest, KML/KMZ course import, the live
map, incidents, lead runner tracking and a browser setup application all work.
Not yet deployed anywhere — it runs locally.

- [`docs/PLAN.md`](docs/PLAN.md) — the plan, decisions, and known gaps
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — event-day procedure for operators
- [`docs/DESIGN.md`](docs/DESIGN.md) — brand, palette and logo decisions

## What it will do

- Show the Full, Half and 10K courses plus aid stations on one map
- Track sweeps, SAG and rovers live, with no page refresh
- Give Net Control a roster panel showing who is on station and who has gone quiet
- Give the Public Safety liaison the same map on a phone, filtered to what matters
- Let NCS drop pickup pins tracked by bib number

## Design notes

- **Receive-only.** This application never transmits. It logs into APRS-IS with
  passcode `-1`, which grants read access and no transmit capability. You need a
  callsign; you do not need a passcode, and should not supply one.
- **APRS-IS rather than a radio/TNC.** This covers operators using phone apps as
  well as RF trackers reaching the network through igates, with no hardware.
- **Good network citizenship.** One connection for the whole server, a server-side
  buddy filter limited to your roster, and backed-off reconnects.
- **Privacy.** The buddy filter means only rostered operators — people who
  consented by signing up — are requested and stored.

## Requirements

Python 3.11+. One runtime dependency (`aprslib`).

## Quick start

```bash
git clone <repo-url> && cd CourseOps
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
cp .env.example .env                       # set APRS_CALLSIGN to your callsign
```

### Set it up in the browser

```bash
courseops serve
```

Then open **http://localhost:8000/setup**. The first visit asks you to create a
system administrator; after that you sign in.

Everything else is forms: create an organization and an event, upload the
organizer's KML or KMZ and assign each feature by looking at it on a map, name
the aid stations and add their What3Words, build the roster, set bib colours,
and copy the access links to send out.

Only two things stay in a terminal, because they happen before the page exists:
the callsign in `.env`, and starting the server.

### Or set it up from the command line

The CLI does the same things and is better for repeat or scripted setup.

Create an event and a roster:

```bash
courseops init-db
courseops add-event marathon2026 "Spring Marathon 2026" \
    --date 2026-04-11 --timezone America/Chicago --lat 34.73 --lon -86.58

# Someone who beacons (sweep following the last runner)
courseops add-station marathon2026 N0CALL-7 "Half-back" --category sweep

# Someone assigned but not beaconing — excluded from the APRS-IS filter and
# from staleness alerting, which is typical for aid station operators
courseops add-station marathon2026 KI4HMD-1 "Aid 4" --category aid_station --no-aprs

courseops roster marathon2026     # shows the roster and the generated APRS-IS filter
```

Import the organizer's course files. Import is additive - the full course, the
half course and the water stops usually arrive as separate files:

```bash
courseops import m2026 SpringMarathon-Full.kmz
courseops review m2026        # lists what was found, with advisory suggestions
```

Nothing becomes a course or an aid station until you say so. Organizer KML is
reliably messy - placemarks named "Untitled Path", routes split across several
segments in arbitrary order and direction, folders mixing water stops with
parking - so each feature is assigned by hand:

```bash
# Stitch several segments into one course; segments drawn backwards are
# reversed automatically, and gaps are reported rather than hidden
courseops assign-course m2026 1 3 --name "Half" --color "#cc3333"

courseops assign-poi m2026 6 --type aid_station --what3words filled.count.soap
courseops discard m2026 2 8 9

courseops courses m2026       # what you ended up with
courseops set-w3w m2026 4 index.home.raft
```

What3Words addresses are entered by hand and maintained by Net Control. There is
no API integration: it is a paid service, so the app validates the shape of an
address but never resolves it. The KML coordinates remain authoritative.

Run the server. It opens one APRS-IS connection and pushes positions to every
browser over a WebSocket:

```bash
courseops serve m2026
```

That prints one link per role. Send each to the right group:

```
  Net Control   http://localhost:8000/e/m2026/KXPbeBeL...
  Liaison       http://localhost:8000/e/m2026/kKUjMiR_...
  Logistics     http://localhost:8000/e/m2026/9fQ2xLmT...
```

These are bearer links - anyone holding one has that role, and there is no
public view. Net Control can write; Liaison (with Public Safety and Medics) and
Logistics (traffic control, cones, teardown) are read-only. Revoke a leaked link
with `courseops revoke-link m2026 <id>` and issue a fresh one with
`courseops links m2026 --new liaison`.

The map is built for a phone held one-handed outdoors: full-bleed map, panels
as bottom sheets, high contrast for daylight, and layer toggles so the field
roles can hide the fixed aid station operators. Station markers differ by shape as
well as colour, and every station shows how long ago it was last heard - a
marker never moves except when a packet actually arrives.

To inspect the feed from the command line instead:

```bash
courseops ingest marathon2026 --max-packets 20    # short smoke test
courseops ingest marathon2026                     # run until Ctrl-C
courseops tail marathon2026 --latest              # newest position per station
```

Categories: `net_control`, `aid_station`, `sweep`, `sag`, `shadow`, `rover`,
`start_finish`.

## Development

```bash
.venv/bin/pytest -q
```

The test suite never touches the network — packet parsing is checked against a
fixture corpus in `tests/fixtures/packets.txt`. When live traffic turns up a
packet that parses wrongly, add the line to that file.

Captured live traffic belongs in `tests/fixtures/live_*.txt`, which is gitignored:
it contains the positions of people who did not consent to being in a public repo.

## License

[MIT](LICENSE)
