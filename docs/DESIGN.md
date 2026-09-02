# Course Ops — Design

**Ham radio event tracking and communications**

A command-center application for ham radio operators supporting events like
races, marathons, and trail runs (e.g. Ragnar-style relays). Shows the event
course/route and provides real-time tracking of hams stationed in the field.

---

## Naming notes

- "Course" was chosen deliberately — it works for race courses, marathon
  courses, and trail-based events alike.
- "Ops" gives it an active, professional, command-center feel (vs. a more
  passive word like "Watch").
- The name reads clean to the general public, but the subtitle clarifies it's
  built specifically for ham radio operators — not generic race-day event
  software.
- "Net" (as in "run the net") is a deliberate nod to ham radio terminology,
  understood by operators but invisible to non-hams.

> Unrelated to the **Logistics** role. "Ops" here is the product name; the roles
> are NCS / Liaison / Logistics.

## Tagline

**Track the course, run the net.** ⭐

Alternates: Every checkpoint, every callsign. · Where the course meets the
airwaves. · Command the course, coordinate the comms. · Real time coverage, real
time comms.

## Logo concepts

**Primary — "Checkpoint Pin with Waveform."** A map pin / checkpoint marker
shape, where the pin's interior (instead of a plain teardrop) is shaped like a
simple oscilloscope wave or small radio tower silhouette.
*Use for:* website header, printed materials, vehicle magnets, race-day banners.

**Icon mark — "C.O. Monogram with Antenna."** The letters C and O set tightly
together as a compact monogram, with a small antenna or signal-bars icon tucked
into or above the lettering.
*Use for:* favicon, app icon, anywhere the full pin logo is too small to read.

## Colour palette (brief)

| Role | Colour | Notes |
|---|---|---|
| Base | Navy / deep blue | Command-center, professional, trustworthy |
| Accent | Safety orange / vest yellow | Ties to race day + field ops visibility |

---

# Implementation

## Two conflicts with field constraints, and how they are resolved

The brief was written for brand identity. Two parts of it collide with
constraints that came from how the app is actually used, so they are resolved
deliberately rather than silently.

### 1. Navy is brand chrome, not the map surface

Navy as the application background would fight the strongest field constraint we
have: **Liaison and Logistics read this on a phone, outdoors, in daylight, for
six hours.** A dark surface loses contrast against ambient glare, and the OSM
map tiles underneath are light regardless — a dark shell around light tiles
reads as a mistake, not a theme.

**Resolution:** navy owns the *chrome* — the top bar, the panel header, the logo
lockup, and marketing surfaces. The map and the station panel stay light. This
is the standard command-center pattern (dark frame, bright working surface) and
it keeps the brand present without costing legibility where it matters.

If a dark map theme is wanted later it should be a **user toggle**, defaulting
to light, and it needs dark map tiles to go with it.

### 2. Safety orange competes with two existing signals

Orange is already doing two jobs in the app:

- `#D55E00` (vermillion) is the first course-line colour from the Okabe-Ito
  palette, so a course line can already be orange.
- `#a35a00` is the **stale** radio status — amber, and close enough to safety
  orange that a viewer could read brand chrome as a warning.

**Resolution:** the brand orange is reserved for **chrome and identity only** —
logo, the accent rule under the top bar, and the active state of a control. It
never appears inside a station row, where amber and red mean something specific.
Course colours keep the Okabe-Ito palette, which is colour-blind safe; the first
course being orange-adjacent is acceptable because course lines sit on the map,
not in the status list.

Rule to keep: **status colour only ever appears on status.**

## Tokens

Defined in `static/app.css` `:root`.

| Token | Value | Use |
|---|---|---|
| `--navy-900` | `#071A2F` | Deepest navy; logo ground |
| `--navy-700` | `#0B2545` | Top bar, panel header, brand surfaces |
| `--navy-500` | `#1B3B6F` | Hover/secondary on navy |
| `--orange` | `#FF6A13` | Safety orange: identity, accent rule, active state |
| `--orange-ink` | `#B34700` | Orange dark enough for text on white (WCAG AA) |
| `--ink` / `--paper` | `#14181d` / `#ffffff` | Working surface, unchanged |
| `--fresh` / `--stale` / `--silent` | green / amber / red | Status only, unchanged |

**Why `--orange-ink` exists:** `#FF6A13` on white is roughly 2.9:1, below the
4.5:1 needed for body text. Safety orange is used as a *surface* and a *mark*,
never as small text on white; where orange text is needed, `--orange-ink` is
used instead.

## Logo assets

Both are inline SVG in `static/` — no image files, no build step, and they
recolour with the CSS tokens.

- `logo-pin.svg` — checkpoint pin whose interior is an oscilloscope trace.
  Used in the top bar next to the event name.
- `favicon.svg` — the compact mark, derived from the **pin**, not from the C.O.
  monogram. See the note below.

Both use navy ground with an orange mark, so they hold up on light and dark.

### The C.O. monogram was tried and rejected for the favicon

The brief specifies a "C.O. monogram with antenna" for the favicon. It was built
and tested at real sizes, and it does not survive: at 16px two tightly-set
letters plus an antenna become an unreadable smudge, the antenna reads as a
stray orange bar disconnected from the mark, and the arcs make it look like a
generic wifi glyph rather than a monogram. Rendered side by side at 16/24/32px,
a 22px **pin** was more legible than a 32px monogram.

The brief's own reasoning is what settles it: the monogram exists for "anywhere
the full pin logo is too small or detailed to read." Testing showed the pin is
the one that reads small, provided its interior is simplified — at 16px a mark
gets about three distinguishable features, so the favicon keeps exactly three:
pin silhouette, light disc, one bold pulse.

The monogram idea is still worth having for contexts where letterforms have room
to work — an app icon at 512px, embroidery, a vehicle magnet. It is just wrong
for a favicon.

## Still to do

- Marketing/landing page (the app itself has no public page by design).
- Printed materials, vehicle magnets, banners — the brief's primary use cases
  for the pin logo. The SVG here is a starting point, not a finished identity.
- A designer pass on the pin silhouette; the current one is geometric rather
  than drawn.
- A C.O. monogram for large-format use (app icon, print, vehicle magnet), where
  letterforms have the room the favicon denied them.
