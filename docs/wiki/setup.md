# Setting up an event: the course

For the club officer who stands the event up, not the volunteer holding a link.
The volunteers' pages are [here](Home).

**Everything here happens in a browser at `/setup`.** Two things stay in a
terminal and nothing else: your club callsign in a file called `.env`, and
starting the server.

> **Setting up, in four parts**
> 1. **The course** - you are here
> 2. [Roster and links](setup-people.md) - the people, and how they get in
> 3. [Race week and after](setup-race-week.md) - tracking, the rehearsal, the report
> 4. [Making it yours](setup-ideas.md) - layers, roles, and what else this can do

![The Events tab: one row per event, with courses, places and roster counts](images/setup-events.png)

The tabs run roughly in the order you will use them. Every tab except
Organizations, Events and Users belongs to **one event** - the one named in
*Working on:* at the top.

The **`?`** in the header opens the right one of these four pages for the tab
you are on, in a new tab.

---

## 1. Organizations, then an event

An **organization** is your club. It owns its events and its administrators and
cannot see another club's - so a second club on the same server is properly
walled off, not just tidily separated.

Then **Events**: a short name, a full name, the date, and the time zone. Get the
time zone right, because every clock in the app and in the after-event report is
drawn in it.

> **The short name is permanent.** It is in `/e/<short-name>/<link>`, which means
> it is in every link you have handed out. The full name can be changed whenever
> you like; the short name cannot, because changing it would 404 every volunteer
> at once, silently, on the morning they need it.

## 2. Import the course

![The Import tab after a file: a map of what was found, and a list to classify](images/setup-import.png)

Drop in the organizer's **KML, KMZ or GPX**. Import is additive - the full
course, the half and the water stops usually arrive as three separate files from
three different tools.

**Nothing becomes a course or a place until you say so.** Everything lands in a
review list, drawn on a map, and you classify it. That is deliberate: organizer
files routinely contain a parking lot named the same as the route, and a
suggestion confident enough to file it as an aid station would be wrong in a way
nobody notices until race day.

The screenshot above is a real export's defects: three placemarks, all named
"Example Marathon", one of them the route and two of them markers. The app says
*looks like course* and *looks like start* and leaves the decision to you.

**Look at the map before you assign anything.** A course split into five
segments, a stray line miles from the route, or a start marker in the wrong car
park is obvious there and invisible in a list of names.

## 3. Courses: colours and draw order

![The Courses tab: name, distance, line colour, bib colour, drag to reorder](images/setup-courses.png)

Each race gets a line colour and a **bib colour**, which are two different
things: the line colour is a map choice, the bib colour is how an aid station
operator says *"first yellow male just came through"*.

Where routes share pavement, the course nearest the top of this list draws on
top. Drag the grip to change it. That is the order everyone starts with; any
viewer can re-stack their own screen without affecting anybody else.

**Check the distance.** If a course reads 3 mi when it should read 13, segments
are missing or one belongs to a different route.

### Leaders we track

![The Leaders we track list: rename, add, delete, drag to reorder](images/setup-leaders.png)

Underneath the courses, on the same tab, is the list of **leaders** - the types
of racer you want a position for as they come round. A new event starts with
*First male* and *First female*. Rename them to your own wording, add a
wheelchair leader or a first junior, delete any your race does not award, and
drag them into the order they get read on the air.

The name is the whole label, exactly as it appears on the Net Control screen,
so type *First wheelchair* rather than *Wheelchair* if that is what will be
said on the net.

**Mind the arithmetic before adding a fourth.** Net Control sees one row per
race per leader, so three races and two leaders is six rows; three races and
four leaders is twelve. Track what will actually be called in.

Two things behave the way the place layers and station roles do:

- **Renaming is always safe.** Reports are recorded against the leader itself,
  not against its wording, so changing *First female* to *First woman*
  mid-event leaves every sighting where it is.
- **A leader that has been sighted cannot be deleted** until its sightings are
  cleared - the app will tell you how many are in the way. Otherwise the
  reports would quietly vanish from the panel with nothing to say where they
  went. Clearing is on the Net Control screen, per race and per leader.

Do this before the event rather than during it.

## 4. Places

![The Places tab: order, mile, name, layer, which races, pin label, coordinates, what3words](images/setup-places.png)

Every point on the map lives here. What each column is for:

- **#** - the club's own running order. Geometry cannot work this out once an
  event has more than one route: each place snaps to whichever line is nearest,
  and where races share road that is a coin flip. Drag or type to set it.
- **Layer** - which kind of place this is. Change one, or tick several and move
  them together, which is what you will do after an import (organizer files
  arrive as one flat list). The layers themselves are yours to invent -
  [see below](setup-ideas.md).
- **Races** - which races this stop serves. State it; do not let it be guessed.
  One water stop routinely serves three races - the organizer's file will
  literally say "WATER (ALL)" - and guessing drops it from every race whose line
  happens to run further away.
- **Pin** - the one or two characters drawn on the pin. Derived from the name,
  and only worth typing when the guess comes out wrong.
- **what3words** - optional, typed in by hand. Aid stations sit at park
  entrances where a street address is useless.

---

**Next:** [Roster and links](setup-people.md) - who is out there, and how they
get in.
