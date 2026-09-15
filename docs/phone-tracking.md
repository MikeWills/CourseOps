# Phone tracking: people without a callsign

Issue #6. Bike medics, race staff and non-licensed drivers have no callsign,
cannot transmit on ham frequencies, and are often the people Net Control most
wants to locate - the medic is who gets sent to a runner down. APRS was the
only tracking mechanism, so they were invisible.

The decision record is the issue itself (three comments, 2026-09-05); this
page is the design as built on 2026-09-15 and the reasoning that has to
survive.

## Why an app and not a web page

A web page cannot track a phone reliably in the background. iOS suspends
JavaScript when the tab is not foreground or the phone locks; Android Chrome
throttles it hard. "Open this link and we will track you" stops the moment
the phone goes in a pocket - and stops *silently*, leaving a dot that looks
live. That is the failure this project refuses everywhere else: a position
that was never reported, which someone will act on.

A dedicated tracking app holds real background-location permission and is
tuned for battery. **OwnTracks** and **Traccar Client** are both free, open
source, on iOS and Android, and post to whatever URL they are given. The
server side is small: receive what they send.

## What was built

- `roster.tracked_by`: `aprs` (default) or `phone`. A phone entry's
  `station_key` is a **designator** (`M1`, `BIKE2`) rather than a callsign;
  it never enters the APRS-IS filter and its base never counts as a rostered
  callsign. `expects_aprs` keeps its meaning - "is silence an alarm" - and
  is on for a phone entry, because the whole point of tracking a medic is
  knowing where they are.
- `event.tracker_token`: one bearer token per event. NULL is off, and the
  endpoint answers 404. The Tracking tab turns it on, resets it, turns it
  off.
- `GET|POST /track/{slug}/{token}` (`field_api.py`): accepts the OsmAnd
  protocol Traccar Client sends (query or form parameters) and OwnTracks'
  HTTP-mode JSON. Answers `[]` with 200 to anything it can read, because the
  app has nothing to act on and an app that gets errors may stop sending. A
  wrong token is 404, never 403, like every other token.
- `tracker.py`: parsing, designator normalisation, the roster match, the
  duplicate and ordering rules, and the QR code.
- The Tracking tab's **Phone tracking** card: the switch, one QR code, the
  three steps, the URL for Traccar users, the designators to type, **Print
  this card**, **Reset the link**.
- The Roster tab's **Tracked by** control: APRS radio / Phone app / Not
  tracked, replacing the "expected to beacon" checkbox. One control because
  the two flags are not independent.

## One URL, one QR, and the person types who they are

Two ways to provision fifteen phones:

**A. One QR per person**, carrying the endpoint and that person's
identifier. Nothing to type; each token separately revocable. But fifteen
squares that have to be cut up, kept in order and handed to the right
person at 6am - and hand Medic 2 the wrong square and they are Medic 1 on
the map, silently and confidently.

**B. One QR for the event; each person types their own designator.**
*Chosen.* One code, printed once, on a card at the briefing table, prepared
weeks ahead. Nothing to collate, nothing to hand to the wrong person.

What B costs, and how each cost is covered:

- **A shared token can post as anybody.** Same posture as the role links,
  and covered the same way the APRS feed covers the public: **the roster is
  the allowlist.** Only a designator matching a roster entry is stored. An
  unknown one is neither dropped nor accepted - it goes to Net Control's
  nearby list (`app.state.nearby`, memory only, sent only to a role holding
  `CAP_SSID`) with the wording *a phone app reporting as MEDIC1*, and the
  Match button binds it to a roster entry exactly as a borrowed radio is
  bound. Nothing is written until then.
- **Typos are the real failure, not attackers.** `Medic1`, `medic 1`,
  `Medic-1` and `Medic_1` are one person; a mismatch makes them invisible
  while their phone transmits happily - the wrong-SSID failure this project
  already has scar tissue for. `tracker.normalise_designator` upper-cases
  and drops spaces, `-` and `_` on BOTH sides of the match, and the roster
  stores the normalised spelling so the card shows exactly what matches.
  Anything left over surfaces in the UI, because a check that has to be
  remembered will be forgotten.
- **Revocation is all-or-nothing.** Resetting the token cuts off every
  phone until they rescan. Accepted for a one-day event; per-person tokens
  (option A) are the answer if that ever stops being acceptable.

## OwnTracks is the default; Traccar Client is accepted

OwnTracks' `owntracks:///config?inline=<base64 .otrc>` is documented in its
booklet as a supported way to configure the app on both platforms - a
public mechanism, which is what a club needs to still work next year. The
QR encodes `{"_type":"configuration","mode":3,"url":...,"monitoring":2,
"locatorInterval":60,"locatorDisplacement":25}`: HTTP mode, the endpoint,
and "move" monitoring, because OwnTracks' default "significant changes"
mode reports every few hundred metres or several minutes on iOS, which is
useless for a medic sent to a runner down. No `tid`: the person sets their
own, which is the whole point.

Traccar Client's deep link exists (`traccar://client?url=...`) but is not a
public contract - the maintainer's guidance is to generate it from the
Traccar server's web app, which is the server we deliberately do not run -
and has unresolved iOS reports against it. So it is not what a club is told
to scan. Its OsmAnd protocol is the easiest thing to receive and its
*Device identifier* takes any text, so it is accepted: type the URL from the
card and the designator into the identifier.

Designators are short (`M1`, `B2`) because OwnTracks' `tid` is conventionally
two characters. Short is better radio practice anyway; the roster row
carries the full label for the map and the panel.

## Verified against the OwnTracks documentation (2026-09-15)

The issue left three things unverified. Two are settled from the booklet:

1. **Which field is the designator.** `tid` - "required for http mode",
   shown as the initials. The endpoint falls back to the last segment of
   `topic` (`owntracks/user/device`, the device name) for an app whose
   `tid` was left blank.
2. **The config link.** `owntracks:///config?inline=` with the base64 of
   the `.otrc` JSON is documented for both apps; `mode: 3` is HTTP and
   `url` the endpoint.
3. **A QR for an app that is not installed** does nothing useful - so the
   guide says install first, and the briefing email a week out matters more
   than the card. Not verified on a phone; expected.

**Not yet done on a real phone:** scanning the printed card, on iOS and on
Android, and watching a fix arrive. That is the rehearsal step for the
first event that uses this, and `docs/PLAN.md` carries it as a gap.

## Offline buffering, and the trap it brings

Both apps buffer fixes while out of coverage and send the backlog when it
returns, so a bike medic riding through a dead zone still delivers their
track - late rather than lost. Three rules follow:

- **The reported timestamp is stored, never arrival time.**
  `tracker.reported_at` reads epoch seconds or milliseconds, ISO 8601, or
  Traccar's `yyyy-MM-dd HH:mm:ss`, into `position.received_at`. Staleness,
  the popup's "last heard" and "newest position" all key off it, so a medic
  coming back into coverage is not drawn as freshly located somewhere they
  left ten minutes ago. A timestamp in the future is clamped to now: a phone
  with a wrong clock must not produce a fix that stays fresh for an hour.
- **Newest by reported time, not by insertion.**
  `db.latest_position_per_station` ranks by `received_at` (window
  function), where it used to take `MAX(id)`. The client also refuses to
  replace a held position with an older one. And the server publishes only
  a fix newer than everything it holds for that station: every fix is
  stored, because the track is real, but the marker never walks backwards
  through a backlog.
- **A resent fix is not a second row.** Same station, same second, same
  source is a duplicate and is skipped.

## Privacy

A ham beaconing on APRS-IS has already published their position; we only
receive it. A medic's phone position is private data we are actively
collecting about an identifiable person. So:

- The switch is off by default and the Tracking tab says, in bold, that it
  collects a person's location from their own phone: ask first, tell them
  when to turn the app off, turn the switch off afterwards.
- Nothing about an unknown designator is written to disk.
- Deleting the event deletes the positions (`ON DELETE CASCADE`), which is
  the deletion-after-the-event half; #4 (archive) is where a gentler
  version of that would live.

## What deliberately does not exist

- **A browser-based tracker** ("open this link"): see the first section. A
  mounted, charged, powered vehicle could use one, but it was not built
  because a second mechanism that works only sometimes is what people
  would reach for first.
- **A Traccar QR.** See above.
- **Per-person tokens.** Option A, kept in reserve.
- **A QR encoder in the browser.** `segno` (pure Python, no dependencies,
  BSD) draws the SVG on the server; nothing is vendored into `static/`.
