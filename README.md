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
v0.1.0 is the first release; see [releases](../../releases/latest) for the Windows download. Not yet run at a live event.

- [`docs/PLAN.md`](docs/PLAN.md) — the plan, decisions, and known gaps
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — event-day procedure for operators
- [`docs/DESIGN.md`](docs/DESIGN.md) — brand, palette and logo decisions
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Apache, TLS and systemd

## What it will do

- Show the Full, Half and 10K courses plus aid stations on one map
- Track sweeps, SAG and rovers live, with no page refresh
- Give Net Control a roster panel showing who is on station and who has gone quiet
- Give the Public Safety liaison the same map on a phone, filtered to what matters
- Let NCS drop pickup pins tracked by bib number, and give SAG their own view of
  the pickup queue - orderable by which one is nearest the vehicle
- Record course notes on the map (a blocked intersection, a confusing turn) for
  the organizer to read after the event
- Put the organizer's own layers on the map - mile markers, medical, traffic
  control, portable toilets - naming each one yourself, with its own icon,
  colour and on/off switch. There is no fixed list and no limit

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

**Python 3.11 or newer**, and nothing else. Five runtime dependencies, installed
for you by the command below: `aprslib`, `defusedxml`, `fastapi`, `uvicorn` and
`python-multipart`. No database server, no npm, no build step.

You also need **your own callsign**. The app connects to APRS-IS to listen; the
passcode stays `-1`, which grants read access and no transmit capability. It
never transmits.

## Getting started

### Windows: download and run

Grab **CourseOps.exe** from the
[latest release](../../releases/latest) and double-click it. One file, nothing
to install, no Python. It prints a setup address, opens no windows of its own,
and keeps its database in `%LOCALAPPDATA%\CourseOps` so nothing is scattered
next to your download.

Windows may warn that the publisher is unknown - the build is not code-signed.

To track people live you also need your callsign in a file named `.env` beside
the executable:

```
APRS_CALLSIGN=W1AW
```

Everything else - the map, the course import, the roster, the whole setup UI -
works without it, and it will tell you that live tracking is off rather than
refusing to start.

A **Raspberry Pi** works too, with no special steps - see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

### Linux, macOS, or a server: pip

`pip` is universal here, so there is no separate download. Six commands from
nothing to a running server - the exact steps, run against a clean clone and an
empty virtualenv.

```bash
git clone <repo-url>
cd CourseOps

python -m venv .venv
.venv/Scripts/python -m pip install -e .     # Linux/macOS: .venv/bin/python

copy .env.example .env                       # Linux/macOS: cp
```

Open `.env` and put your callsign in it:

```
APRS_CALLSIGN=W1AW
```

That is the only file you ever edit by hand. Then start it:

```bash
.venv/Scripts/courseops serve                # Linux/macOS: .venv/bin/courseops
```

It creates its own database on first run - there is no separate setup command -
and prints where to go:

```
  Course Ops
  Setup: http://localhost:8000/setup
         (first run - it will ask you to create an administrator)

  Listening on http://127.0.0.1:8000   Ctrl-C to stop
```

If you get **"APRS_CALLSIGN is still the placeholder N0CALL"**, you copied the
file but have not edited it yet - that is the step above.

### Then do the rest in a browser

Open **http://localhost:8000/setup**. The first visit asks you to create a
system administrator; after that you sign in.

Everything else is forms: create an organization and an event, upload the
organizer's KML or KMZ and assign each feature by looking at it on a map, name
the aid stations and add their What3Words, build the roster, set bib colours,
and copy the access links to send out.

Only two things stay in a terminal, because they happen before the page exists:
the callsign in `.env`, and starting the server.

### Reaching it from a phone on the same wifi

By default it listens on localhost only, which no other device can reach. To let
phones on your network in:

```bash
.venv/Scripts/courseops serve --host 0.0.0.0
```

Then browse to `http://<your computer's IP>:8000/...` from the phone. Find the
IP with `ipconfig` on Windows or `ip addr` on Linux. You may have to allow the
port through the firewall.

**One thing will not work over plain http:** the "you are here" dot, and so the
SAG queue's "nearest me" ordering. Browsers only allow geolocation in a secure
context, and localhost is the sole exception. Everything else works normally.
That is the reason for the next section rather than a limitation you can
configure away.

### Sharing it without a web server

You do not need Apache, a domain or a certificate to let volunteers in. A tunnel
gives you a public HTTPS address pointing straight at the app on your own
laptop, and it takes one command. HTTPS is not cosmetic here - it is what makes
the "where am I" dot and SAG's *nearest me* ordering work at all, neither of
which a plain LAN can do.

Two free options, both fine for a one-day event.

**Tailscale** - stable address, only your machine installs anything:

```bash
tailscale funnel 8000     # public: the address volunteers use
tailscale serve  8000     # your own devices only: for testing on your phone
```

Use `serve` while you are setting up and walking the course, `funnel` on the
day. The first `tailscale funnel` may send you to the admin console to switch
Funnel on for your tailnet - do that before race week.

**Cloudflare Tunnel** - no account at all:

```bash
cloudflared tunnel --url http://localhost:8000
```

Either way, start the app pointed at the address the tunnel gave you:

```bash
courseops serve <event> --behind-proxy --base-url https://<tunnel-host>
```

`--behind-proxy` keeps the `Secure` flag on admin session cookies, since the
tunnel terminates TLS and speaks plain HTTP to the app. `--base-url` makes the
role links it prints carry the tunnel address instead of `localhost` - those
links are what you are about to send to fifteen people.

Three things to know before choosing:

- A **Cloudflare quick tunnel takes a new hostname every time it starts.** If
  the laptop reboots at mile 6, every link you handed out is dead and you are
  re-sending URLs while running a net. Tailscale's address is stable.
- **ngrok works, but not on the free tier for this.** Free ngrok shows every
  visitor a click-through warning page first. You will click through once while
  setting up, get a cookie that hides it for seven days, and never see it
  again - so it looks fine right up until your volunteers meet it at 6am and
  conclude the link is broken. A paid plan removes it.
- A tunnel is **a dependency you do not control on race morning.** For one
  event that is usually a fair trade against configuring Apache. For a club
  running this every year, a real server is sturdier.

`docs/DEPLOYMENT.md` compares all of them in a table and covers what a tunnel
does not change - role links are bearer tokens, so a public tunnel widens the
audience from your wifi to anyone holding the URL.

### On a real web server

`docs/DEPLOYMENT.md` walks through it: Apache as a reverse proxy, a Let's
Encrypt certificate, and a systemd unit so it comes back after a reboot. Ready
made files are in `deploy/`.

Three things are easy to get wrong, so they are called out there:

- Apache needs **`mod_proxy_wstunnel`**, and the `/ws/` rules must come *before*
  the catch-all - otherwise the map loads and then never moves, with no error.
- Run with **`--behind-proxy`** or session cookies silently lose the `Secure`
  flag, because behind a proxy the app cannot see the real scheme.
- Bind to **127.0.0.1** so nobody can reach it bypassing TLS.

For one club on one afternoon, a laptop with `--host 0.0.0.0` is genuinely
enough - you just lose the location dot.

### Or set it up from the command line

The CLI does the same things and is better for repeat or scripted setup.

Create an event and a roster:

```bash
courseops init-db
courseops add-event marathon2026 "Spring Marathon 2026" \
    --date 2026-04-11 --timezone America/Chicago --lat 34.73 --lon -86.58

# The callsign alone is enough. The SSID belongs to whichever radio or phone
# app they bring on the day, so the app binds the entry to the first SSID it
# hears that looks like a person rather than a digipeater.
courseops add-station marathon2026 N0CALL "Half-back" --category sweep

# Give an SSID yourself only when you know it and want to pin it
courseops add-station marathon2026 N0CALL-7 "Full-back" --category sweep

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
  SAG           http://localhost:8000/e/m2026/Nl0s3QBM...
  Liaison       http://localhost:8000/e/m2026/kKUjMiR_...
  Logistics     http://localhost:8000/e/m2026/9fQ2xLmT...
```

These are bearer links - anyone holding one has that role, and there is no
public view. Permission is per capability rather than one write flag:

| Role | Can change |
|---|---|
| Net Control | Everything |
| SAG | The pickup queue only - en route, picked up, dropped off, and the bib |
| Liaison | Nothing. Embedded with Public Safety and Medics |
| Logistics | Nothing. Traffic control, cones, teardown |

SAG is scoped deliberately: a bearer link lives in a moving vehicle, so a lost
phone should cost one incident queue rather than the roster and the links.
Revoke a leaked link
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
