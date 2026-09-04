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
| **SAG** | SAG link | The pickup queue: mark a runner en route, picked up and dropped off, and fill in the bib. Can order pickups by how near they are. Nothing else is editable |
| **Liaison** | Liaison link | View only. Embedded with Public Safety / Medics |
| **Logistics** | Logistics link | View only. Traffic control, cone placement, teardown |

Each role gets its **own link**. Anyone holding a link has that role — treat
them like door keys. Send each to the right group and no other.

---

## One to two weeks before

> **Setup is done in the browser.** Start the server with `courseops serve`,
> then open **/setup** and sign in. The steps below name the equivalent CLI
> command in each case, because the command line is still there and is quicker
> if you are repeating a setup you have done before.
>
> On the very first run, `/setup` asks you to create a system administrator.
> After that: create an organization for your club, then an event inside it.

### 1. Get the course files from the organizer

Ask for **KML or KMZ**. If they can only send GPX, that is not supported yet
(GitHub issue #1) — ask them to export KML, or send the file to the developer.

Expect several files (full course, half, 10K, water stops) exported from
different tools. That is normal; import is additive.

Also ask explicitly for **aid station locations**. Course exports usually do not
contain them — the Mankato file had none — so plan to place them by hand.

### 2. Create the event

**In the browser:** Organizations → create one for your club, then Events →
New event.

<details><summary>Same thing from the command line</summary>

```bash
courseops init-db
courseops add-event mankato2026 "2026 Mankato Marathon" \
    --date 2026-10-18 --timezone America/Chicago --lat 44.16 --lon -94.00
```
</details>

`--lat/--lon` set the default map centre. Roughly right is fine; the map fits to
the course once one is imported.

### 3. Import and review the course

**In the browser:** the Course tab. Choose the KML or KMZ, and every feature it
found is drawn on a map. Click a line or a pin — on the map or in the list — and
say what it is.

Look at the map before assigning anything. This is where a course split into
five pieces, a stray line miles from the route, or a folder mixing water stops
with parking becomes obvious, and none of it is visible from the names alone.

<details><summary>Same thing from the command line</summary>

```bash
courseops import mankato2026 MankatoFull.kml
courseops review mankato2026 --verbose
```
</details>

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

NCS maintains these. In `/setup` -> Aid stations, every place shows its
coordinates and a `///` link beside the What3Words box. The link opens the
what3words map already centred on that square, so the job is: click `///`,
copy the three words, paste them back. No hunting for the point on a map.

Or from the command line:

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

**Post aid station operators at their station.** Most never beacon, so this is
the only way the app knows where they are:

```bash
courseops courses mankato2026            # find the aid station's id
courseops post mankato2026 KI4HMD-1 4    # post that operator at it
```

**Name aid stations however your club does** - Alpha/Beta/Gamma, NATO phonetic,
numbers, or place names. Lists are ordered by position along the course, not
alphabetically, so the naming scheme does not matter and the order always
matches the direction of travel.

**Get `--no-aprs` right.** If operators who were never going to beacon are
listed as APRS-tracked, the "who has gone quiet" panel fills with false alarms
and people stop reading it within twenty minutes. That panel is only useful
because it is short.

Confirm the roster and the filter it produces:

```bash
courseops roster mankato2026
```

### 5a. Name your layers before importing

Setup -> **Layers**. Whatever kinds of place this event has: mile markers,
medical, traffic control, portable toilets. Add as many as you need, name each
in your own words, and give it an icon and a colour - each becomes a switch on
the map.

**Expect everything to arrive in one layer.** Organizer KML is usually a flat
list rather than a folder per kind of place, so after importing you sort it:
Setup -> **Aid stations**, tick the rows that belong together, and move them to
the right layer in one go. Do this before naming things, so you are naming
within a layer rather than hunting through a mixed list.

The one setting that matters operationally is **"We staff these"**. Tick it when
we put an operator at that kind of place and track them. Staffed layers can have
someone posted to them from the Roster tab and can report a lead runner; an
unstaffed layer is just pins you can turn on and off. Medical starts unticked,
because a medic tent is usually run by the race's own medical team rather than
by one of ours - tick it if your club staffs them.

Turn **On by default** off for anything numerous, such as a 26-pin mile marker
layer, so the map does not open cluttered.

**Station roles** on the same tab can be renamed. If your club says "Floater"
rather than "Rover", change it - the status wording follows the role, not the
name, so nothing else moves.

### 6a. Check who is actually on the air

**The app watches for this on its own.** If someone transmits on an SSID the
roster does not name, a **Needs attention** panel appears at the top of the NCS
view with two choices: *"This is <their label>"*, which repoints the roster at
the SSID they are really using, or *Ignore*, for a digipeater or igate. A count
badge shows on the Layers button so it is visible on a phone without opening the
panel.

That means nothing has to be remembered on race morning. The check below is
still worth running beforehand, because it is better to fix this at a club
meeting than while the net is live.

**Do this at a club meeting a week out, not on race morning.**

**Collect callsigns, not SSIDs.** Enter `WX0MIK` on the roster and leave the
SSID off. The app binds the entry to the first SSID it hears that looks like a
person rather than a digipeater, and the Roster tab then shows *heard as
WX0MIK-5* beside the callsign. This is the recommended way to build a roster:
volunteers know their callsign, but the SSID belongs to whichever radio or phone
app they bring on the day, so SSIDs collected weeks in advance include some
wrong ones. Enter an SSID yourself only when you know it and want to pin it.

The check below is still worth running, because it tells you what is on the air
before the event rather than during it.

```bash
courseops check-in mankato2026 --seconds 300
```

It listens for five minutes and reports every SSID heard under each rostered
callsign, telling you which look like people and which look like digipeaters or
igates. Fix any wrong SSIDs it finds, then dismiss the infrastructure:

```bash
courseops ignore mankato2026 WX0MIK-7 --reason digipeater
courseops ignored mankato2026
```

Ignoring is permanent for that event and takes effect immediately.

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

### Add it to the phone's home screen

Worth doing for anyone in the field. It opens full screen with no browser bar,
which is a real gain on a phone held one-handed all morning, and it saves
hunting for the link in a text thread.

- **iPhone/iPad:** open the link in Safari, then Share, then *Add to Home Screen*
- **Android:** open the link in Chrome, then the menu, then *Add to Home screen*

The icon is labelled with the **role** - "Net Control", "SAG", "Liaison", "Logistics" -
so someone holding two links can tell them apart.

> **If a phone is lost or stolen, revoke that role's link.** Installing saves the
> access link onto the home screen, so whoever has the phone has the role. Use
> `courseops revoke-link`, then issue a replacement with `courseops links
> <event> --new <role>` and redistribute.

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

### Reading course position

Where a station is along the route is shown as **mile 14.2** in its row, in
place of the callsign, and in full in its popup: *"mile 14.2 of Full"* plus how
far it has left to run.

This is the number to read on the net, and it is what closes the event down: as
the sweep passes each aid station, that station can tear down and Logistics can
start pulling cones behind it.

Three things to know:

- **The course name is always shown with the mile.** Where the Full, Half and
  10K share road, a station is matched to whichever line it happens to be
  nearest — so trust the pair, not the number alone.
- **A station well off the route shows its callsign instead of a mile.** That is
  deliberate: no figure is better than a wrong one. It usually means they have
  genuinely left the course.
- **Accuracy follows the course file.** A route hand-drawn in a consumer tool
  cuts corners with straight-line shortcuts, so it measures short and the mile
  figures drift. A course from a GIS system does not have this problem. If mile
  figures look consistently off, suspect the course file before the app.

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
aprs.fi)? Is the callsign on the roster, spelled right? Do they have cell data
where they are? If the roster names an exact SSID, check it is the one they are
really using - `N0CALL-9` and `N0CALL-7` are different radios. Removing the SSID
from the roster entry lets the app find it on its own.

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

### Closing aid stations

The sweep's mile marker is what tells NCS which aid stations can tear down and
which roads can reopen. Watch the sweep's mile figure, then set each aid station
it has passed to *Torn down*.

### Lead runners

Track the first male and first female for each race. Aid stations call it in as
the leaders pass; NCS records it.

Before the event, set each race's bib colour so it matches what operators will
see on the runners:

```bash
courseops bib-color mankato2026 1 --color "#ffcc00" --name Yellow
```

On the day, when a station reports *"first yellow male, bib 101, just passed
Bravo"*:

1. Type the bib in the small box (it stays filled in, so usually it is already
   right).
2. Tap **Passed <station>** — the button already names the station they were
   expected at next.

Use the **At...** picker if they passed a different station, and **Undo** to
remove a mis-tap.

Once two sightings exist, the panel shows a pace and an estimate for the next
station — which is what tells an aid station when to be ready.

> **If the pace looks absent, that is deliberate.** When two reports are entered
> close together — catching up after a busy net — the arithmetic produces a
> nonsense pace, so the app shows none rather than an estimate you might plan
> around. The sighting itself is still recorded.

### Pickups and incidents

When a pickup is called in:

1. Tap **+ Drop a pickup pin**, then tap the map where the runner is.
2. The incident opens straight away as **Reported** and the cursor lands in the
   bib box. Type the bib if you have it — if not, leave it and fill it in later.
   Add a short note if it helps ("waiting at mile 9").
3. As it progresses, tap **En route**, **Picked up**, then **Closed**.

The list puts whatever needs attention first: unanswered reports at the top,
and within each status the one that has been waiting longest. The time shown is
**how long it has been in its current status** — so a report reading `8m` has
been sitting undispatched for eight minutes. That is the number to watch.

Closed incidents come off the map but stay in the list, so the map shows only
live work.

Liaison and Logistics see every incident and its status, live, but cannot change
anything.

> **Keep notes operational.** Bib, location, status and something like "unable
> to continue, waiting at mile 9" are what the net needs. Do not record a
> runner's medical condition — the note field is deliberately short, and the app
> never holds the bib-to-name mapping.

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
