# AprsWebTracker

A web map for marathon-style events: race courses and aid stations from the
organizer's KML/KMZ files, overlaid with live APRS positions of the ham radio
operators supporting the event.

There are phone apps, and there are full situational-awareness platforms like TAK.
This aims at the gap between them — something a radio club can stand up for a race
without much effort.

**Status: early development.** Phase 1 (APRS-IS ingest) works. There is no web
interface yet. See [`docs/PLAN.md`](docs/PLAN.md) for the full plan.

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
git clone <repo-url> && cd AprsWebTracker
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
cp .env.example .env                       # set APRS_CALLSIGN to your callsign
```

Create an event and a roster:

```bash
awt init-db
awt add-event marathon2026 "Spring Marathon 2026" \
    --date 2026-04-11 --timezone America/Chicago --lat 34.73 --lon -86.58

# Someone who beacons (sweep following the last runner)
awt add-station marathon2026 N0CALL-7 "Half-back" --category sweep

# Someone assigned but not beaconing — excluded from the APRS-IS filter and
# from staleness alerting, which is typical for aid station operators
awt add-station marathon2026 KI4HMD-1 "Aid 4" --category aid_station --no-aprs

awt roster marathon2026     # shows the roster and the generated APRS-IS filter
```

Run the ingest and watch what arrives:

```bash
awt ingest marathon2026 --max-packets 20    # short smoke test
awt ingest marathon2026                     # run until Ctrl-C
awt tail marathon2026 --latest              # newest position per station
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

## Licensing note

Not yet licensed. Pick one before making the repository public.
