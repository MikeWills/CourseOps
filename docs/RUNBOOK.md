# Event Runbook

Operating procedure for using Course Ops at a real event. Written for the
person setting it up and for Net Control, not for developers.

> **Draft status.** Sections marked **[CLUB]** need your club's own practice
> filled in — they are placeholders, not recommendations. Sections marked
> **[PHASE n]** describe features not built yet; ignore them until that phase
> lands.

---

## Who does what

| Role | Link | Can |
|---|---|---|
| **Net Control (NCS)** | NCS link | See everything, edit What3Words, set station status, manage incidents **[PHASE 6]** |
| **Liaison** | Liaison link | View only. Embedded with Public Safety / Medics |
| **Logistics** | Logistics link | View only. Traffic control, cone placement, teardown |

Each role gets its **own link**. Anyone holding a link has that role — treat
them like door keys. Send each to the right group and no other.

---

## One to two weeks before

### 1. Get the course files from the organizer

Ask for **KML or KMZ**. If they can only send GPX, that is not supported yet
(GitHub issue #1) — ask them to export KML, or send the file to the developer.

Expect several files (full course, half, 10K, water stops) exported from
different tools. That is normal; import is additive.

Also ask explicitly for **aid station locations**. Course exports usually do not
contain them — the Mankato file had none — so plan to place them by hand.

### 2. Create the event

```bash
courseops init-db
courseops add-event mankato2026 "2026 Mankato Marathon" \
    --date 2026-10-18 --timezone America/Chicago --lat 44.16 --lon -94.00
```

`--lat/--lon` set the default map centre. Roughly right is fine; the map fits to
the course once one is imported.

### 3. Import and review the course

```bash
courseops import mankato2026 MankatoFull.kml
courseops review mankato2026 --verbose
```

**Review every row.** The suggestions are advisory and deliberately cautious.
Organizer files routinely contain placemarks named "Untitled Path", routes split
across several segments in arbitrary order, and folders mixing water stops with
parking and porta-johns.

Then assign each feature:

```bash
# One course from several segments; backwards ones are reversed automatically
courseops assign-course mankato2026 1 3 --name "Full"
courseops assign-course mankato2026 4   --name "Half"

courseops assign-poi mankato2026 6 --type aid_station --name "Aid 1"
courseops assign-poi mankato2026 2 --type start --name "Start"
courseops discard    mankato2026 8 9        # parking, porta-johns, junk
```

**Check the distance it reports.** If a course reads 3 mi when it should read
13, segments are missing or one belongs to a different route. If `assign-course`
warns about a gap, look at the course on the map before the event.

### 4. Set colours and draw order

```bash
courseops courses mankato2026
courseops style-course mankato2026 1 --color "#cc3333"
courseops style-course mankato2026 2 --order 10     # higher draws on top
```

Courses share road for miles. Draw order decides which line wins where they
overlap — put the one people ask about most on top.

### 5. Enter What3Words for each aid station

NCS maintains these. Look each one up at what3words.com and type it in:

```bash
courseops set-w3w mankato2026 4 index.home.raft
```

Worth the effort: three words survive a voice radio net far better than a
lat/lon when you are directing someone to a road intersection.

### 6. Build the roster

Every assigned operator, whether or not they beacon APRS:

```bash
# Someone who beacons — a sweep following the last runner
courseops add-station mankato2026 N0CALL-7 "Full-back" --category sweep --operator "Jane"

# Assigned but not beaconing — typical for aid station operators.
# --no-aprs keeps them off the APRS-IS filter AND out of staleness alerts.
courseops add-station mankato2026 KI4HMD-1 "Aid 4" --category aid_station --no-aprs
```

Categories: `net_control`, `aid_station`, `sweep`, `sag`, `shadow`, `rover`,
`start_finish`.

**Get `--no-aprs` right.** If operators who were never going to beacon are
listed as APRS-tracked, the "who has gone quiet" panel fills with false alarms
and people stop reading it within twenty minutes. That panel is only useful
because it is short.

Confirm the roster and the filter it produces:

```bash
courseops roster mankato2026
```

### 7. Test it end to end

```bash
courseops serve mankato2026 --no-ingest
```

Open each link. Check the course draws, aid stations appear with their
What3Words, and the roster lists everyone. **[CLUB]** _Decide who does this
check and when._

---

## Race morning

### Start the server

```bash
courseops serve mankato2026
```

This opens **one** APRS-IS connection and prints the three role links.

Confirm before going live:

- [ ] The map loads on the NCS workstation
- [ ] The status badge top-right reads **Live** (green)
- [ ] The course and aid stations are drawn
- [ ] At least one mobile station appears within a few minutes

### Send the links

Send each role link to that group only. **[CLUB]** _Which channel — group text,
email, printed card?_

Tell recipients:

- The map is view-only for them
- Their own location dot is private — it is never sent to the server or seen by
  anyone else
- Positions update on their own; **do not refresh**

---

## During the event

### Setting station status

Each station row has two lines. The top line is **radio status**, derived
automatically from the feed. The second line is **operational status**, which
NCS sets by tapping — and the two are deliberately independent:

| Category | Buttons |
|---|---|
| Aid station | Not staffed / On station / Torn down |
| Sweep, SAG, rover | Not started / Rolling / Finished |
| Shadow | Not started / Assigned / Released |

"Aid 1 — no APRS — On station" is a perfectly healthy row. "SAG 1 — silent 16
min — Rolling" is the one that should worry you. Never read one line as if it
were the other.

Closed stations dim and sink to the bottom of the list, so what is still running
stays at the top.

**Type your initials** in the Operator box at the bottom of the panel, once per
shift. They are stamped on every status change with a timestamp, so a handover
can see who marked Aid 3 torn down and when. It is a note in a log, not a login.

Changes appear on the Liaison and Logistics views within a second; they cannot
change anything themselves.

### What NCS watches

The **station list** sorts with whatever needs attention first: silent, then
stale, then fresh, with non-beaconing operators last. Each row shows how long
ago the station was last heard.

| Colour | Means |
|---|---|
| Green | Heard within 10 minutes — normal |
| Amber | 10–20 minutes — worth a radio check |
| Red | Over 20 minutes, or never heard |
| Hollow | Not tracked by APRS — expected, not a problem |

**Quiet is normal.** Phone apps beacon every 1–5 minutes and rural stretches
have cell dead zones. A four-minute-old position is the ordinary state, not a
fault. A marker only moves when a packet actually arrives, so it will sit still
and then jump — that is deliberate. The app never guesses a position between
reports.

### What the field roles see

Aid station operators and net control are hidden by default on the Liaison and
Logistics views, so the map stays readable on a phone. Sweeps and SAG stay
visible — for Logistics that is the point: **the sweep is the back of the pack,
so its position is what says a road segment is clear and the cones can come up.**

Any layer can be switched back on from the Layers panel; the choice sticks on
that phone.

### The location button

The ◎ button bottom-right shows a blue dot at the viewer's own position and
follows them as they move. Tap again to turn it off.

If it does not work, the message says why:

| Message | Cause |
|---|---|
| Location needs HTTPS | The server is on plain `http://`. Browsers block location outside HTTPS except on localhost. See Known limitations. |
| Location permission denied | The browser blocked it; re-allow in site settings |
| No GPS fix yet | Usually indoors — wait or step outside |
| Location approximate (±___ m) | A wifi-derived fix, not GPS. Do not treat it as exact |

### Common situations

**A station never appears.** In order: are they beaconing at all (check
aprs.fi)? Is the callsign on the roster with the exact SSID — `N0CALL-9` and
`N0CALL-7` are different radios? Do they have cell data where they are?

**Everyone goes silent at once.** That is the server's connection, not the
field. Check the status badge; if it says Reconnecting, the app is already
retrying with backoff. Do not restart it repeatedly — APRS-IS bans clients that
reconnect in a tight loop.

**A viewer's map looks stale.** Have them check the badge. If it says
Reconnecting, they are in a dead zone and the map is frozen; it resyncs fully
when they get signal back.

**A link leaks to the wrong people.**

```bash
courseops list-links mankato2026
courseops revoke-link mankato2026 3
courseops links mankato2026 --new logistics    # issue a replacement
```

Revoking one role's link does not affect the others.

### Closing aid stations **[PHASE 5]**

Once course-relative position lands, the sweep's mile marker is what tells NCS
which aid stations can tear down and which roads can reopen.

### Pickups and incidents **[PHASE 6]**

Dropping a pin for a runner pickup, tracked by bib number.

---

## After the event

```bash
# Ctrl-C to stop the server
```

The SQLite database keeps every position and raw packet, so the event can be
reviewed later **[PHASE 7]**. Back up the database file — it is the whole
record.

**[CLUB]** _Where do backups go? Who keeps them and for how long?_

Consider revoking all links after the event:

```bash
courseops list-links mankato2026
```

---

## Known limitations

Tell volunteers these up front; each one otherwise reads as a bug.

- **The location dot needs HTTPS.** Served over plain `http://` on a LAN,
  browsers block geolocation entirely. Everything else still works.
- **A ham with no cell data is invisible.** APRS-IS only sees what reaches the
  internet, whether from a phone app or from an RF tracker through an igate. No
  coverage means no dot, and the app cannot tell the difference between that and
  a radio that is switched off.
- **Positions are not continuous.** Updates arrive every few minutes at best.
- **Receive only.** The app never transmits and cannot send APRS messages.
- **Aid station operators usually do not appear as moving markers** — most do
  not beacon. They are drawn at their assigned location.
- **Hand-drawn courses cut corners.** Routes built in consumer tools may use
  straight-line shortcuts instead of following roads, which will bias mile
  figures once **[PHASE 5]** lands. GIS-produced courses should not have this.

---

## Quick reference

```bash
courseops serve <event>                     # run it (server + APRS-IS)
courseops serve <event> --no-ingest         # map only, no APRS connection
courseops links <event>                     # show the three role links
courseops list-links <event>                # link status and last use
courseops revoke-link <event> <id>          # kill a leaked link
courseops roster <event>                    # who is assigned, and the APRS filter
courseops courses <event>                   # courses and aid stations
courseops tail <event> --latest             # newest position per station
```
