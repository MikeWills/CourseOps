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

## Brand artwork

`docs/brand/` holds the finished artwork, generated with an image model from
the brief above and then traced into vector:

- `logo-lockup.svg` - pin, wordmark and tagline, navy and orange on
  transparent. For the README, the wiki, banners and print.
- `icon.svg` - the square app icon: white pin with the tower and signal
  arcs on a navy ground, the course winding out from under it. Full bleed,
  square corners, the road running off the bottom edge - the platforms
  apply their own mask.
- `source/` - the raster originals the traces came from. `icon.png` is also
  what `tools/make_icons.py` rasterises the home-screen icons from.

The traces are made with [vtracer](https://github.com/visioncortex/vtracer):
the raster is first quantised to exactly three colours (so the tracer sees
hard edges and no anti-aliasing fringe), traced in spline mode, and every
fill is then snapped to the tokens below. Flat two-colour art traces
cleanly; anything with gradients or soft edges would not, which is one more
reason the brief forbids them. A future PNG from the same model goes
through the same steps.

## Logo assets (in the app)

The in-app mark is the pin from the app icon - white ring, navy disc, two
orange signal arcs a side, a lattice tower - **redrawn by hand** as a few
SVG paths rather than cropped from the trace. The trace cannot give a clean
pin: the road runs behind the pin's tip, so any crop carries fragments of
it. Three files in `static/`, no build step:

- `logo-pin.svg` - white ring, for the navy chrome: the top bar and the
  panel head.
- `logo-pin-ink.svg` - the same pin with the ring in navy, for a light
  surface: the sign-in page. An `<img>` cannot take a CSS token, so a
  second file is cheaper than inlining the SVG in three places.
- `favicon.svg` - the pin on a rounded navy square at 82% of the frame, no
  road. `tools/make_icons.py` draws the PNG favicons to the same shape.
- `logo-lockup.svg` - a copy of `docs/brand/logo-lockup.svg`, shown on the
  sign-in page. `logo-lockup-reversed.svg` is the same with white where the
  artwork is navy, for the panel head in the live app (the sheet on a
  phone, the left column on a wide screen) and for the setup bar.
  Regenerate the reversed copy with `sed 's/#0B2545/#FFFFFF/g'` when the
  lockup changes; do not edit it by hand. The top bar keeps the pin beside
  the name as live text in Overpass: at 27px the wordmark would not read,
  and text scales, recolours and reaches a screen reader.

### The favicon carries more than three features now, on purpose

The earlier favicon kept exactly three features - pin, disc, one pulse -
because at 16px on a 1x display that is all a mark gets. The pin from the
icon has a ring, arcs and a tower, and at 16px on a 1x display it IS a
smudge. That is accepted: the home-screen icon and the favicon are now the
same mark, which is what a person recognises the tab by after installing
the app, and nearly every screen this runs on is hi-DPI, where the tab
icon is the 32px rendering and reads. If a 1x laptop becomes the common
case, the fix is a simplified 16px variant, not a different mark.

### The C.O. monogram was tried and rejected for the favicon

The brief specifies a "C.O. monogram with antenna" for the favicon. It was built
and tested at real sizes, and it does not survive: at 16px two tightly-set
letters plus an antenna become an unreadable smudge, the antenna reads as a
stray orange bar disconnected from the mark, and the arcs make it look like a
generic wifi glyph rather than a monogram. Rendered side by side at 16/24/32px,
a 22px **pin** was more legible than a 32px monogram.

The monogram idea is still worth having for contexts where letterforms have room
to work - embroidery, a vehicle magnet. It is just wrong for a favicon.

## Icon set

One SVG is not enough. Three platform facts drive the file list:

1. **iOS ignores SVG for home screen icons, and ignores the web manifest too.**
   It reads `<link rel="apple-touch-icon">` and the `apple-mobile-web-app-*`
   metas. Without a PNG there, adding Course Ops to a home screen produces a
   blurry screenshot of the page instead of an icon.
2. **Android maskable icons are cropped to a launcher-chosen shape** - circle,
   squircle, teardrop - and only the central 80% is guaranteed. A maskable icon
   drawn edge to edge gets shaved. The maskable variants therefore draw the mark
   *smaller*; that is correct, not an error.
3. **Neither platform wants your corner radius.** Both apply their own mask, so
   the full-bleed sources have square corners. Baking in a different radius
   shows as a double-rounded edge. Only the browser-tab favicons, which are
   never masked, carry their own corners.

| File | Size | Purpose |
|---|---|---|
| `favicon.svg` | vector | Modern browsers |
| `favicon.ico` | 16/32/48 | Legacy `/favicon.ico` requests |
| `favicon-16/32/48.png` | 16, 32, 48 | Browser tab, bookmarks |
| `apple-touch-icon.png` | 180 | iOS home screen (full bleed, iOS masks) |
| `icon-192.png`, `icon-512.png` | 192, 512 | Manifest, purpose `any` |
| `icon-maskable-192/512.png` | 192, 512 | Manifest, purpose `maskable`, 80% safe zone |

Regenerate with `python tools/make_icons.py` (needs `pip install pillow`;
Pillow is not a runtime dependency). The home-screen icons are rasterised
from `docs/brand/source/icon.png`, every pixel snapped to the three brand
colours first so the padded navy matches; the favicons are drawn
geometrically to the shape of `favicon.svg`. Everything is supersampled 4x,
so no native SVG rasteriser is required.

## Installing to a home screen

The manifest is served per role at
`/api/{event}/{token}/manifest.webmanifest`, not as a static file, because the
app has **no tokenless entry point** - a fixed `start_url` would install a
shortcut to a 404. `start_url` and `scope` are the caller's own role link, so
"Add to Home Screen" lands on the right event with the right permissions.

`short_name` is the **role**, not the product: home screen labels truncate around
twelve characters, and "Net Control" / "Logistics" is the useful half when
someone holds links for two roles.

`display: standalone` means it opens without browser chrome, which is worth
having on a phone held one-handed for six hours.

> **Consequence to know:** installing puts the bearer token on that phone's home
> screen. That is consistent with the link model - anyone holding the link has
> the role - but it means a lost phone is a link to revoke. Covered in
> `docs/RUNBOOK.md`.

## Type

**Overpass**, with **Overpass Mono** for log data. Overpass is drawn from the
FHWA Highway Gothic alphabet - the face on the county road signs the
volunteers stand beside - which is the one typographic reference this app
has any claim to. The mono cut carries what a ham writes in a log: callsigns,
bib numbers, times, miles. It has a slashed zero, which is the practical win:
`W0RRC` and `WORRC` stop looking alike.

Both are shipped from `static/fonts/` (about 60 KB together, Latin subset,
variable weight), never fetched from a font CDN: a field phone should not be
sending requests to a third party to draw the pickup queue. `font-display:
swap` means the system face stands in until the file arrives, and the fallback
stack is the old system-ui one. Italic is not shipped; the app barely uses it.
Both faces are SIL Open Font License 1.1; the licence ships beside them as
`static/fonts/OFL.txt`.

Two rules follow from the type:

- **Sentence case, set heavy, for headings.** Small tracked capitals were the
  previous treatment for section and panel heads; they read as labels about
  the content rather than the content, and they washed out in glare. Heads
  are now 15px/800 in ink.
- **Mono is for values, never labels.** A callsign, a bib, an age, a mile.
  The word beside it stays in Overpass.

## Palette, revised

Two colours plus status. `--accent` was a third blue (`#0b5fa5`) filling
buttons; it is now an alias of `--navy-700`, so the primary action is the brand
colour. Ink is navy-derived (`#0e1b2b`, soft `#46566a`) rather than neutral
grey, and setup's page ground is a pale tint of it (`--ground`, `#e9eef4`).
Corner radius is one token (`--radius`, 6px): squarer than a phone app,
because the reference is signage. Status colours are untouched.

## The bib tag

The one loud element. In the pickup queue the bib is set as a race bib -
mono numerals in a squared tag with a thick band on the left in the STATUS
colour, the same colour the row's square dot carried before, so nothing new
is said, only said larger. It is what an operator matches against the runner
in front of them. Course notes keep their round dot: a note has no status and
no bib, and the shape is what says so.

## Setup on a phone

Below 700px - the map's own phone tier - the tab bar becomes one row that
scrolls sideways, and every table becomes a stack of cards, one per row, each
cell labelled with its column heading. The cells are the same elements laid
out differently, not a second rendering: `labelTableCells()` in `setup.js`
copies each heading into `data-label` on its cells as a table lands, and the
CSS prints it. So `bindSaveAll`, the drag handles and every per-row control
keep working untouched, and a tenth table gets the phone layout for free.

Cells lay out with flex-wrap rather than grid, because a cell can hold several
things - a mile and its course, two coordinate boxes and a copy button, a row
of race checkboxes - and they should keep flowing inline after the label the
way they did across the desktop column, wrapping under it only when too wide.

## The front door

`/` redirects to `/setup`, where the sign-in carries the lockup and the
tagline. There is no public page beyond it by design: the field roles arrive
by link and never see it. The marketing page the brief asked for is still
open - it belongs off the app entirely.

## Still to do

- Marketing/landing page, off the app (the app itself has no public page by
  design; `/` only leads to sign-in).
- Printed materials, vehicle magnets, banners — the brief's primary use cases
  for the pin logo. The SVG here is a starting point, not a finished identity.
- A designer pass on the pin silhouette; the current one is geometric rather
  than drawn.
- A C.O. monogram for large-format use (app icon, print, vehicle magnet), where
  letterforms have the room the favicon denied them.
