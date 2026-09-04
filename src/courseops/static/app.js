/* Course Ops map client.
 *
 * Three rules drive this file, all from how the app is actually used:
 *
 * 1. Marker positions are NEVER interpolated. Reports arrive every 1-5 minutes
 *    with gaps; a smoothly-animated marker would show a position that was never
 *    reported, and someone would act on it. Discrete jumps are honest and are
 *    also easier on a phone battery over six hours.
 * 2. Age is always visible. A station quiet for 4 minutes is normal; one quiet
 *    for 18 is an alarm. The two must never look alike.
 * 3. On reconnect we resync the whole state rather than replaying messages. A
 *    phone back from a dead zone gets a correct picture, not a plausible one.
 */
'use strict';

const M = (() => {
  const parts = location.pathname.split('/').filter(Boolean);   // ['e', slug, token]
  return { slug: parts[1], token: parts[2] };
})();

const KM_PER_MILE = 1.609344;
const METERS_PER_FOOT = 0.3048;

const state = {
  event: null,
  role: null,
  canWrite: false,
  // What this role may actually change, from the server. A button the server
  // would refuse is worse than no button at all.
  can: new Set(),
  incidentKinds: [
    {value: 'pickup', label: 'Pickup'},
    {value: 'note', label: 'Course note'},
  ],
  // Which kind the next dropped pin becomes.
  pinKind: 'pickup',
  // Where we are, for ordering the pickup queue by proximity. Local only.
  mePosition: null,
  // 'waiting' (longest unanswered first) or 'near' (closest first).
  sortPickupsBy: 'waiting',
  thresholds: { stale_after_s: 600, silent_after_s: 1200 },
  roster: new Map(),          // station_key -> roster row
  positions: new Map(),       // station_key -> latest position
  markers: new Map(),         // station_key -> L.Marker
  courseLayers: new Map(),    // course id -> L.Polyline
  poiLayers: new Map(),       // category key -> L.LayerGroup
  poiCategories: [],          // the club's own layer definitions
  visibleCourses: new Set(),
  layerPrefs: {},
  socket: null,
  reconnectDelay: 1000,
  opStatuses: ['pending', 'active', 'closed'],
  incidents: new Map(),       // id -> incident
  incidentMarkers: new Map(), // id -> L.Marker
  incidentStatuses: [
    {value: 'reported', label: 'Reported'},
    {value: 'en_route', label: 'En route'},
    {value: 'picked_up', label: 'Picked up'},
    {value: 'closed', label: 'Closed'},
  ],
  droppingPin: false,
  ssidAlerts: [],
  leaders: [],
  divisions: [{value: 'male', label: 'First male'},
              {value: 'female', label: 'First female'}],
  aidStations: [],
  operatorInitials: '',
  following: false,
  meMarker: null,
  meAccuracy: null,
  geoWatchId: null,
};

/* ---------- preferences (per browser, survive a reload or phone lock) ---- */

const PREF_KEY = `awt:${M.slug}:prefs`;

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
  } catch (e) {
    return {};                                  // private mode, blocked storage
  }
}

function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({
      layers: state.layerPrefs,
      courses: [...state.visibleCourses],
      initials: state.operatorInitials,
      sortPickupsBy: state.sortPickupsBy,
    }));
  } catch (e) { /* not worth bothering the user about */ }
}

/* Per-role layer defaults. NCS sees everything; the field roles start with the
   30-odd fixed aid station operators hidden, which is the difference between a
   readable phone screen and an unreadable one.

   Both field roles keep SWEEPS on, and for Logistics that is the whole point:
   the sweep is the back of the pack, so its position is what says a road
   segment is clear and the cones can come up. */
function defaultLayers(role) {
  const field = role === 'liaison' || role === 'logistics';
  return {
    aid_station_ops: !field,
    sweep: true,
    sag: true,
    shadow: true,
    rover: true,
    net_control: !field,
    start_finish: true,
  
  };
}

/* Station roles. Fixed keys, because each carries its own status wording; the
   displayed names come from the server so a club can use its own terminology.
   Layers for PLACES are not here at all - those are the club's own list and
   arrive with the state. */
const LAYER_LABELS = {
  aid_station_ops: 'Aid station operators',
  sweep: 'Sweeps',
  sag: 'SAG',
  shadow: 'Shadows',
  rover: 'Rovers',
  net_control: 'Net control',
  start_finish: 'Start / finish stations',

};

const CATEGORY_TO_LAYER = {
  aid_station: 'aid_station_ops',
  sweep: 'sweep',
  sag: 'sag',
  shadow: 'shadow',
  rover: 'rover',
  net_control: 'net_control',
  start_finish: 'start_finish',
};

/* ---------- formatting -------------------------------------------------- */

const mph = (kmh) => (kmh == null ? null : kmh / KM_PER_MILE);
const feet = (m) => (m == null ? null : m / METERS_PER_FOOT);
const miles = (m) => (m == null ? null : m / 1609.344);

function ageSeconds(iso) {
  if (!iso) return null;
  const then = Date.parse(iso.endsWith('Z') ? iso : iso + 'Z');
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 1000);
}

/* "mile 14.2" is what the net says. One decimal on purpose: the course geometry
   is not accurate to better than that, and more digits would imply precision we
   do not have. */
function formatMile(meters) {
  if (meters == null) return null;
  return `mile ${(meters / 1609.344).toFixed(1)}`;
}

function coursePositionOf(stationKey) {
  const position = state.positions.get(stationKey);
  if (position && position.course_position) return position.course_position;
  // An operator posted at an aid station has no packets of their own - most
  // never beacon - so their position comes from the station they are posted at.
  const entry = state.roster.get(stationKey);
  return entry ? (entry.course_position || null) : null;
}

function formatAge(seconds) {
  if (seconds == null) return '--';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h${String(mins % 60).padStart(2, '0')}`;
}

/* Radio status is derived and automatic. It is deliberately separate from the
   operational status NCS sets by hand: "on station, no APRS" is healthy. */
function radioStatus(stationKey) {
  const entry = state.roster.get(stationKey);
  if (entry && !entry.expects_aprs) return 'no_aprs';
  const position = state.positions.get(stationKey);
  if (!position) return 'silent';
  const age = ageSeconds(position.received_at);
  if (age == null) return 'silent';
  if (age > state.thresholds.silent_after_s) return 'silent';
  if (age > state.thresholds.stale_after_s) return 'stale';
  return 'fresh';
}

function categoryOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry ? entry.category : 'rover';
}

/* Permission is per capability rather than one write flag, because SAG can
   work the pickup queue and must not be able to rewrite the roster or revoke a
   link. The server enforces the same table; this only decides what to draw. */
/* Straight-line distance in metres. Deliberately not driving distance: we have
   no routing engine, and on a closed course the road you would actually take is
   not something this app knows. So the figure is labelled "away" rather than
   presented as a drive, and it orders the list rather than promising an ETA. */
function metresBetween(a, b) {
  const R = 6371000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function distanceToMe(incident) {
  if (!state.mePosition) return null;
  return metresBetween(state.mePosition, {lat: incident.lat, lon: incident.lon});
}

function can(capability) {
  return state.can.has(capability);
}

function labelOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry ? entry.display_label : stationKey;
}

/* The operator's name, for calling them by it on the air.

   "K0JZP, Alaric" gets attention that "K0JZP" alone does not - someone half
   listening, or hard of hearing, catches their own name when they miss a
   callsign. So the name is shown wherever a station is identified, never on
   its own: the callsign is what is legally required and what other stations
   are listening for. */
function operatorOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry && entry.operator_name ? entry.operator_name : '';
}

function initials(text) {
  const words = String(text || '').replace(/[^\w\s-]/g, ' ').trim().split(/[\s-]+/);
  if (!words[0]) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/* ---------- map --------------------------------------------------------- */

const map = L.map('map', { zoomControl: false, attributionControl: true });
L.control.zoom({ position: 'topright' }).addTo(map);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);

map.on('dragstart', () => setFollowing(false));

/* Below this, aid station names overlap each other into an unreadable smear;
   at or above it a screen holds a mile or two of course and at most a couple
   of stations. The characters on the pins stay visible at every zoom - it is
   only the full names that come and go. */
const NAME_LABEL_MIN_ZOOM = 14;

function nameLabelsWanted() {
  // Off unless asked for. The characters on the pins are the answer to
  // "which one is that?"; the full names are a second opinion, and a map
  // carrying both all the time is busier than the job needs.
  return state.layerPrefs['poi-names'] === true;
}

function syncNameLabels() {
  map.getContainer().classList.toggle(
    'show-poi-names',
    nameLabelsWanted() && map.getZoom() >= NAME_LABEL_MIN_ZOOM);
}
map.on('zoomend', syncNameLabels);
syncNameLabels();

function stationIcon(stationKey) {
  const status = radioStatus(stationKey);
  const category = categoryOf(stationKey);
  const shape = category === 'sweep' ? ' stn--sweep'
              : category === 'sag' ? ' stn--sag' : '';
  const size = category === 'sweep' || category === 'sag' ? 30 : 26;
  return L.divIcon({
    className: '',
    html: `<div class="stn stn--${status}${shape}">${escapeHtml(initials(labelOf(stationKey)))}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function stationPopup(stationKey) {
  const position = state.positions.get(stationKey);
  const entry = state.roster.get(stationKey);
  const status = radioStatus(stationKey);
  const rows = [];

  if (entry) rows.push(['Callsign', stationKey]);
  // Directly under the callsign, because the two are read out together.
  if (entry && entry.operator_name) rows.push(['Operator', entry.operator_name]);
  if (status === 'no_aprs') {
    rows.push(['Radio', 'not tracked by APRS']);
  } else if (position) {
    const age = ageSeconds(position.received_at);
    rows.push(['Last heard', `${formatAge(age)} ago`]);
    if (position.speed_kmh != null) {
      rows.push(['Speed', `${Math.round(mph(position.speed_kmh))} mph`]);
    }
    if (position.altitude_m != null) {
      rows.push(['Altitude', `${Math.round(feet(position.altitude_m))} ft`]);
    }
    const located = position.course_position;
    if (located) {
      // Course name always travels with the mile: where routes share road the
      // station snaps to whichever line is nearest, which is a coin flip.
      rows.push(['Course position',
        `${formatMile(located.distance_along_m)} of ${located.course_name}`]);
      rows.push(['Remaining',
        `${(located.remaining_m / 1609.344).toFixed(1)} mi`]);
      if (located.offset_m > 60) {
        // Worth surfacing: either they have left the route, or the course line
        // cuts a corner here and the road does not.
        rows.push(['Off the line', `${Math.round(located.offset_m)} m`]);
      }
    }
    rows.push(['Position', `${position.lat.toFixed(5)}, ${position.lon.toFixed(5)}`]);
    if (position.comment) rows.push(['Comment', position.comment]);
  } else {
    rows.push(['Radio', 'no position yet']);
  }

  return `<h3>${escapeHtml(labelOf(stationKey))}</h3><dl>` +
    rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('') +
    '</dl>';
}

/* Escapes quotes as well as angle brackets. The textContent->innerHTML trick
   does NOT escape " or ', which makes it unsafe the moment a value lands inside
   an attribute. Everything interpolated into markup below goes through here. */
function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function stationVisible(stationKey) {
  const layer = CATEGORY_TO_LAYER[categoryOf(stationKey)] || 'rover';
  return state.layerPrefs[layer] !== false;
}

function upsertStationMarker(stationKey) {
  const position = state.positions.get(stationKey);
  if (!position) return;

  let marker = state.markers.get(stationKey);
  if (!marker) {
    marker = L.marker([position.lat, position.lon], {
      icon: stationIcon(stationKey),
      title: labelOf(stationKey),
    });
    marker.bindPopup(() => stationPopup(stationKey));
    state.markers.set(stationKey, marker);
  } else {
    // setLatLng, not an animated transition: see rule 1 at the top.
    marker.setLatLng([position.lat, position.lon]);
    marker.setIcon(stationIcon(stationKey));
  }

  if (stationVisible(stationKey)) {
    if (!map.hasLayer(marker)) marker.addTo(map);
  } else if (map.hasLayer(marker)) {
    map.removeLayer(marker);
  }
}

function drawCourses(courses) {
  state.courseLayers.forEach((layer) => map.removeLayer(layer));
  state.courseLayers.clear();

  // Ascending sort_order = draw order, so the highest ends up on top where
  // routes share road. That ordering is the operator's control for overlap.
  courses.forEach((course) => {
    const latlngs = course.geojson.coordinates.map(([lon, lat]) => [lat, lon]);
    const line = L.polyline(latlngs, {
      color: course.color || '#D55E00',
      weight: 5,
      opacity: 0.9,
      dashArray: course.dash_pattern || null,
    });
    line.bindPopup(
      `<h3>${escapeHtml(course.name)}</h3><dl><dt>Distance</dt>` +
      `<dd>${miles(course.distance_m).toFixed(1)} mi</dd></dl>`
    );
    state.courseLayers.set(course.id, line);
    if (state.visibleCourses.has(course.id)) line.addTo(map);
  });
}

/* One Leaflet layer per category, so every layer can be switched
   independently. The set is whatever the club defined - there is no fixed list
   here and no limit on how many. */
function drawPois(pois) {
  state.poiLayers.forEach((layer) => map.removeLayer(layer));
  state.poiLayers = new Map();
  state.poiCategories.forEach((cat) => state.poiLayers.set(cat.key, L.layerGroup()));

  pois.forEach((poi) => {
    const cat = categoryOfPoi(poi);
    /* Two characters if this layer is labelled, and the layer glyph if not.
       They cannot both fit: a pin is 24px, and text that does not fit is
       worse than no text.

       So a labelled pin gives up its glyph, which is why labels are per
       layer rather than global. On a labelled layer the colour and the
       popup carry which layer it is; on every other layer the glyph still
       does. In practice one layer is labelled - the aid stations - and
       telling THOSE apart is the whole point. */
    const label = cat.show_labels ? (poi.label_text || '') : '';
    const marker = L.marker([poi.lat, poi.lon], {
      icon: L.divIcon({
        className: '',
        // The glyph carries the meaning and the colour reinforces it, so two
        // layers a club happens to colour alike are still told apart.
        html: `<div class="poi-icon" style="background:${cssColor(cat.color)}">`
            + (label
                ? `<span class="poi-label">${escapeHtml(label)}</span>`
                : glyphSvg(cat.icon, 14))
            + '</div>',
        // A labelled pin is bigger than a glyph pin. A glyph reads as a shape
        // at 24px; two characters at that size are a smudge held at arm's
        // length in sunlight, which is the condition this is read in.
        iconSize: label ? [30, 30] : [24, 24],
        iconAnchor: label ? [15, 15] : [12, 12],
      }),
      title: `${poi.name} (${cat.name})`,
      /* A labelled pin draws above an unlabelled one. Leaflet stacks markers
         by latitude, so a mile marker a few metres north of an aid station
         sits on top of it and hides exactly the character someone turned
         labels on to read. Labelling a layer is a statement that those pins
         matter more than the ones around them. */
      zIndexOffset: label ? 1000 : 0,
    });
    const rows = [['Layer', cat.name]];
    if (poi.course_position) {
      rows.push(['Course position',
        `${formatMile(poi.course_position.distance_along_m)} of ` +
        `${poi.course_position.course_name}`]);
    }
    if (poi.what3words) rows.push(['what3words', `///${poi.what3words}`]);
    if (poi.notes) rows.push(['Notes', poi.notes]);
    rows.push(['Position', `${poi.lat.toFixed(5)}, ${poi.lon.toFixed(5)}`]);
    marker.bindPopup(
      `<h3>${escapeHtml(poi.name)}</h3><dl>` +
      rows.map(([k, v]) => {
        const cls = k === 'what3words' ? ' class="w3w"' : '';
        return `<dt>${escapeHtml(k)}</dt><dd${cls}>${escapeHtml(String(v))}</dd>`;
      }).join('') + '</dl>'
    );
    /* Two characters answer "which one is that?" only if you already know
       the scheme. Zoomed in far enough that they will not collide, show the
       name as well - the pin says F, the label beside it says Foxtrot.

       Permanent rather than on hover: this is read on a phone in a field,
       where there is no hover. Visibility is by CSS class on the map
       container rather than by adding and removing tooltips, so zooming does
       not churn a hundred DOM nodes. */
    if (label && poi.name) {
      marker.bindTooltip(poi.name, {
        permanent: true,
        direction: 'right',
        offset: [17, 0],
        className: 'poi-name',
        // Never intercept a tap. The pin and the course underneath it are
        // both things someone is trying to hit.
        interactive: false,
      });
    }

    const layer = state.poiLayers.get(cat.key);
    if (layer) layer.addLayer(marker);
  });

  state.poiLayers.forEach((layer, key) => {
    if (poiLayerVisible(key)) layer.addTo(map);
  });
}

/* A club's own default for the layer, unless this browser has been told
   otherwise. A 26-marker mile layer wants to start off; aid stations always
   start on. */
function poiLayerVisible(key) {
  const pref = state.layerPrefs[`poi:${key}`];
  if (pref !== undefined) return pref !== false;
  const cat = state.poiCategories.find((c) => c.key === key);
  return cat ? cat.visible !== false : true;
}

function categoryOfPoi(poi) {
  return state.poiCategories.find((c) => c.key === poi.poi_type)
    || {key: poi.poi_type, name: poi.poi_type, icon: 'pin', color: null};
}

/* Set via a style property, never interpolated into an attribute: an invalid
   value is dropped by the CSS parser instead of becoming markup. */
function cssColor(value) {
  return /^#[0-9a-fA-F]{3,8}$/.test(String(value || '')) ? value : '#35507a';
}

function fitToContent() {
  const bounds = L.latLngBounds([]);
  state.courseLayers.forEach((line) => bounds.extend(line.getBounds()));
  state.markers.forEach((marker) => bounds.extend(marker.getLatLng()));
  if (bounds.isValid()) {
    map.fitBounds(bounds, { padding: [40, 40] });
  } else if (state.event && state.event.center_lat != null) {
    map.setView([state.event.center_lat, state.event.center_lon], state.event.zoom || 13);
  } else {
    map.setView([39.5, -98.35], 4);
  }
}

/* ---------- panel ------------------------------------------------------- */

function renderCourseToggles(courses) {
  const host = document.getElementById('course-list');
  host.innerHTML = '';
  if (!courses.length) {
    host.innerHTML = '<p class="muted">No courses imported yet.</p>';
    return;
  }
  courses.forEach((course) => {
    const row = document.createElement('label');
    row.className = 'toggle';
    row.innerHTML =
      `<input type="checkbox" ${state.visibleCourses.has(course.id) ? 'checked' : ''}>` +
      '<span class="swatch"></span>' +
      `<span class="label">${escapeHtml(course.name)}</span>` +
      `<span class="meta">${miles(course.distance_m).toFixed(1)} mi</span>`;
    // Set via the style property rather than interpolating into an attribute:
    // the CSS parser drops an invalid value instead of it becoming markup.
    row.querySelector('.swatch').style.background = course.color || '#D55E00';
    row.querySelector('input').addEventListener('change', (ev) => {
      const line = state.courseLayers.get(course.id);
      if (ev.target.checked) {
        state.visibleCourses.add(course.id);
        if (line) line.addTo(map);
      } else {
        state.visibleCourses.delete(course.id);
        if (line) map.removeLayer(line);
      }
      savePrefs();
    });
    host.appendChild(row);
  });
}

function layerToggle(label, checked, onChange, swatch) {
  const row = document.createElement('label');
  row.className = 'toggle';
  row.innerHTML =
    `<input type="checkbox" ${checked ? 'checked' : ''}>` +
    (swatch || '') +
    `<span class="label">${escapeHtml(label)}</span>`;
  row.querySelector('input').addEventListener('change', (ev) => onChange(ev.target.checked));
  return row;
}

/* Two lists: the people (fixed roles, club-named) and the places (the club's
   own layers, however many they have made). Places are rendered from the state
   rather than a constant here, which is the whole point - a mile marker layer
   and a traffic control layer are as first-class as aid stations. */
function renderLayerToggles() {
  const host = document.getElementById('layer-list');
  host.innerHTML = '';

  Object.keys(LAYER_LABELS).forEach((key) => {
    host.appendChild(layerToggle(
      roleLabel(key),
      state.layerPrefs[key] !== false,
      (on) => {
        state.layerPrefs[key] = on;
        applyLayerVisibility();
        savePrefs();
      },
    ));
  });

  const places = document.getElementById('place-layer-list');
  places.innerHTML = '';
  document.getElementById('place-layer-section').hidden = !state.poiCategories.length;

  /* One switch for the names, above the layers, because it is not a layer -
     it changes how the labelled layers draw rather than whether they draw.
     Zoom still gates it: switched on at the whole-course view the names
     would overlap into a smear, which is why the pins carry characters. */
  places.appendChild(layerToggle(
    'Place names (zoomed in)',
    nameLabelsWanted(),
    (on) => {
      state.layerPrefs['poi-names'] = on;
      syncNameLabels();
      savePrefs();
    },
  ));
  state.poiCategories.forEach((cat) => {
    const swatch = `<span class="layer-glyph" style="color:${cssColor(cat.color)}">`
      + `${glyphSvg(cat.icon, 16)}</span>`;
    places.appendChild(layerToggle(
      cat.name,
      poiLayerVisible(cat.key),
      (on) => {
        state.layerPrefs[`poi:${cat.key}`] = on;
        applyLayerVisibility();
        savePrefs();
      },
      swatch,
    ));
  });
}

/* A club renames its roles, so the label is never hardcoded on the client. */
function roleLabel(key) {
  const found = (state.roleLabels || {})[key];
  return found || LAYER_LABELS[key] || key;
}

function applyLayerVisibility() {
  state.markers.forEach((_, stationKey) => upsertStationMarker(stationKey));
  state.poiLayers.forEach((layer, key) => {
    if (poiLayerVisible(key)) {
      if (!map.hasLayer(layer)) layer.addTo(map);
    } else if (map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
  });
}

/* Stations sort with whatever needs attention first: silent before stale
   before fresh, and anything not expected to beacon last, since a quiet aid
   station operator is not news. */
const STATUS_RANK = { silent: 0, stale: 1, fresh: 2, no_aprs: 3 };

/* Operational status is NCS's manual state and is NEVER merged with radio
   status. "On station, no APRS" is a healthy row; "Rolling, silent 18 min" is an
   alarm. Collapsing them into one badge loses exactly the distinction that makes
   this panel worth watching. */
const OP_RANK = { active: 0, pending: 1, closed: 2 };

function opStatusOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry ? (entry.op_status || 'pending') : 'pending';
}

function opLabelOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry ? (entry.op_status_label || entry.op_status || '') : '';
}

/* Category-specific wording: an aid station is "Torn down", a sweep "Finished".
   The server sends the label for the CURRENT status; this table covers the
   other buttons. */
const OP_LABELS_BY_CATEGORY = {
  aid_station:  { pending: 'Not staffed', active: 'On station', closed: 'Torn down' },
  sweep:        { pending: 'Not started', active: 'Rolling',    closed: 'Finished' },
  sag:          { pending: 'Not started', active: 'Rolling',    closed: 'Finished' },
  rover:        { pending: 'Not started', active: 'Rolling',    closed: 'Finished' },
  shadow:       { pending: 'Not started', active: 'Assigned',   closed: 'Released' },
  net_control:  { pending: 'Not open',    active: 'Open',       closed: 'Closed' },
  start_finish: { pending: 'Not staffed', active: 'Staffed',    closed: 'Closed' },
};
const GENERIC_OP_LABELS = { pending: 'Pending', active: 'Active', closed: 'Closed' };

function opLabelFor(stationKey, value) {
  const table = OP_LABELS_BY_CATEGORY[categoryOf(stationKey)] || GENERIC_OP_LABELS;
  return table[value] || GENERIC_OP_LABELS[value];
}

async function setStationStatus(stationKey, opStatus) {
  const entry = state.roster.get(stationKey);
  if (!entry) return;
  const previous = entry.op_status;
  const previousLabel = entry.op_status_label;

  // Optimistic: on a race-morning link the round trip is visible otherwise.
  entry.op_status = opStatus;
  entry.op_status_label = opLabelFor(stationKey, opStatus);
  renderStations();

  try {
    const response = await fetch(
      `/api/${M.slug}/${M.token}/station/${encodeURIComponent(stationKey)}/status`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op_status: opStatus, changed_by: state.operatorInitials }),
      }
    );
    if (!response.ok) throw new Error(String(response.status));
  } catch (err) {
    // Roll back. Leaving NCS believing an aid station was marked torn down when
    // the server never got it is worse than showing the failure.
    entry.op_status = previous;
    entry.op_status_label = previousLabel;
    renderStations();
    setLocateStatus('Could not save status - check the connection');
  }
}

function renderStations() {
  const host = document.getElementById('station-list');
  const keys = [...new Set([...state.roster.keys(), ...state.positions.keys()])]
    .filter(stationVisible)
    .sort((a, b) => {
      // Closed sinks: a torn-down aid station going silent is not news, and
      // leaving it near the top would bury the rows that matter.
      const byOp = OP_RANK[opStatusOf(a)] - OP_RANK[opStatusOf(b)];
      if (byOp !== 0) return byOp;
      const byStatus = STATUS_RANK[radioStatus(a)] - STATUS_RANK[radioStatus(b)];
      if (byStatus !== 0) return byStatus;
      // Course order beats alphabetical: "Aid 10" sorts before "Aid 2" by name,
      // and Greek letters come out Alpha, Beta, Delta, Epsilon, Gamma. Course
      // order is also how NCS works through them, behind the sweep.
      const pa = coursePositionOf(a);
      const pb = coursePositionOf(b);
      if (pa && pb) return pa.distance_along_m - pb.distance_along_m;
      if (pa) return -1;
      if (pb) return 1;
      return labelOf(a).localeCompare(labelOf(b));
    });

  document.getElementById('station-count').textContent =
    keys.length ? `(${keys.length})` : '';

  if (!keys.length) {
    host.innerHTML = '<p class="muted">No stations on the roster yet.</p>';
    return;
  }

  host.innerHTML = '';
  keys.forEach((stationKey) => {
    const status = radioStatus(stationKey);
    const position = state.positions.get(stationKey);
    const age = position ? ageSeconds(position.received_at) : null;
    const ageClass = status === 'no_aprs' ? 'age--none'
                   : status === 'silent' ? 'age--silent'
                   : status === 'stale' ? 'age--stale' : '';
    const ageText = status === 'no_aprs' ? 'no APRS'
                  : position ? formatAge(age) : 'never';

    const op = opStatusOf(stationKey);
    const row = document.createElement('div');
    row.className = `station station--op-${op}`;

    const locate = document.createElement('button');
    locate.type = 'button';
    locate.className = 'station-main';
    // The mile figure replaces the callsign when we have one: on a phone the
    // row has space for one of them, and "mile 14.2" is what gets said on the
    // radio. The callsign is still in the popup.
    const located = coursePositionOf(stationKey);
    const middle = located
      ? `<span class="mile" title="${escapeHtml(located.course_name)}">` +
        `${escapeHtml(formatMile(located.distance_along_m))}</span>`
      : `<span class="call">${escapeHtml(stationKey)}</span>`;

    // The operator's name earns a second line only when there is one, so an
    // event that never fills them in looks exactly as it did before.
    const who = operatorOf(stationKey);
    const nameCell = who
      ? `<span class="name">${escapeHtml(labelOf(stationKey))}` +
        `<span class="who">${escapeHtml(stationKey)} · ${escapeHtml(who)}</span>` +
        `</span>`
      : `<span class="name">${escapeHtml(labelOf(stationKey))}</span>`;

    locate.innerHTML =
      `<span class="dot dot--${status}"></span>` +
      nameCell +
      middle +
      `<span class="age ${ageClass}">${escapeHtml(ageText)}</span>`;
    locate.addEventListener('click', () => {
      const marker = state.markers.get(stationKey);
      if (marker) {
        setFollowing(false);
        map.setView(marker.getLatLng(), Math.max(map.getZoom(), 15));
        marker.openPopup();
      }
    });
    row.appendChild(locate);

    // Second axis on its own line, so it can never be misread as radio status.
    const opRow = document.createElement('div');
    opRow.className = 'station-op';
    if (can('stations')) {
      state.opStatuses.forEach((value) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `op-btn${value === op ? ' op-btn--on' : ''}`;
        btn.textContent = opLabelFor(stationKey, value);
        btn.setAttribute('aria-pressed', String(value === op));
        btn.addEventListener('click', () => setStationStatus(stationKey, value));
        opRow.appendChild(btn);
      });
    } else {
      const span = document.createElement('span');
      span.className = 'op-readonly';
      span.textContent = opLabelOf(stationKey);
      opRow.appendChild(span);
    }
    row.appendChild(opRow);

    host.appendChild(row);
  });
}

/* ---------- SSID mismatches ---------------------------------------------- */

/* Someone signs up as WX0MIK-1 and their phone beacons WX0MIK-5. The filter
   asks for every SSID so they are tracked either way, but the roster label is
   wrong and their own digipeater may be on the map too.

   This is surfaced here, unprompted, rather than in a command someone has to
   remember to run. The failure it catches is silent, and anything that must be
   remembered will eventually be forgotten - especially on race morning. */

async function resolveSsid(path, body) {
  try {
    const response = await fetch(`/api/${M.slug}/${M.token}/ssid/${path}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || String(response.status));
    }
    await loadState();          // the roster changed; resync rather than patch
  } catch (err) {
    setLocateStatus(`Could not update: ${err.message}`);
  }
}

function renderSsidAlerts() {
  const section = document.getElementById('ssid-section');
  const host = document.getElementById('ssid-alerts');
  const badge = document.getElementById('sheet-badge');
  const alerts = state.ssidAlerts || [];

  section.hidden = alerts.length === 0;
  badge.hidden = alerts.length === 0;
  badge.textContent = alerts.length ? String(alerts.length) : '';
  if (!alerts.length) return;

  host.innerHTML = '';
  alerts.forEach((alert) => {
    const box = document.createElement('div');
    box.className = 'ssid-alert';

    const head = document.createElement('div');
    head.className = 'ssid-head';
    head.innerHTML =
      `<span class="ssid-key">${escapeHtml(alert.station_key)}</span>` +
      `<span class="ssid-meta">${escapeHtml(alert.symbol)} · ` +
      `${alert.packets} pos</span>`;
    box.appendChild(head);

    const why = document.createElement('p');
    why.className = 'ssid-why';
    why.textContent = alert.looks_like_infrastructure
      ? 'Transmitting under a rostered callsign, but looks like fixed equipment.'
      : 'Transmitting under a rostered callsign, but this SSID is not on the roster.';
    box.appendChild(why);

    if (!can('ssid')) {
      const note = document.createElement('p');
      note.className = 'muted';
      note.textContent = 'Net Control can resolve this.';
      box.appendChild(note);
      host.appendChild(box);
      return;
    }

    const actions = document.createElement('div');
    actions.className = 'ssid-actions';

    // Adopting is offered per roster entry sharing the callsign, so the label
    // says who it actually is rather than making NCS work it out.
    alert.roster_candidates.forEach((candidate) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ssid-adopt';
      button.textContent = `This is ${candidate.display_label}`;
      button.title = `Point ${candidate.station_key} at ${alert.station_key}`;
      button.addEventListener('click', () => resolveSsid('adopt', {
        from_station_key: candidate.station_key,
        to_station_key: alert.station_key,
      }));
      actions.appendChild(button);
    });

    const ignore = document.createElement('button');
    ignore.type = 'button';
    ignore.className = 'ssid-ignore';
    ignore.textContent = alert.looks_like_infrastructure
      ? 'Ignore (equipment)' : 'Ignore';
    ignore.addEventListener('click', () => resolveSsid('ignore', {
      station_key: alert.station_key,
      reason: alert.symbol,
    }));
    actions.appendChild(ignore);

    box.appendChild(actions);
    host.appendChild(box);
  });
}

/* ---------- lead runners ------------------------------------------------- */

/* The counterpart to the sweep: the sweep says when an aid station may close,
   the leader says when it has to be ready.

   We only ever learn this when a runner passes an operator who calls it in, so
   the primary control is a single button naming the station they are expected
   at next. On race day that has to be one tap while holding a microphone;
   picking from a list is the correction path, not the normal one. */

function formatEta(seconds) {
  if (seconds == null) return null;
  const mins = Math.round(seconds / 60);
  if (mins < 1) return 'due now';
  if (mins < 60) return `~${mins} min`;
  return `~${Math.floor(mins / 60)}h${String(mins % 60).padStart(2, '0')}`;
}

function paceLabel(mps) {
  if (!mps) return null;
  // Runners think in minutes per mile, never in metres per second.
  const minPerMile = 1609.344 / mps / 60;
  if (!isFinite(minPerMile) || minPerMile > 60) return null;
  const mins = Math.floor(minPerMile);
  const secs = Math.round((minPerMile - mins) * 60);
  return `${mins}:${String(secs).padStart(2, '0')}/mi`;
}

async function recordSighting(leader, poiId, bib) {
  try {
    const response = await fetch(`/api/${M.slug}/${M.token}/leaders/sighting`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        course_id: leader.course_id, division: leader.division,
        poi_id: poiId, bib: bib || null, changed_by: state.operatorInitials,
      }),
    });
    if (!response.ok) throw new Error(String(response.status));
  } catch (err) {
    setLocateStatus('Could not record the sighting');
  }
}

async function undoSighting(leader) {
  try {
    await fetch(`/api/${M.slug}/${M.token}/leaders/undo`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({course_id: leader.course_id, division: leader.division}),
    });
  } catch (err) {
    setLocateStatus('Could not undo');
  }
}

function renderLeaders() {
  const host = document.getElementById('leader-list');
  if (!state.leaders.length) {
    host.innerHTML = '<p class="muted">No courses imported yet.</p>';
    return;
  }

  host.innerHTML = '';
  let lastCourse = null;
  state.leaders.forEach((leader) => {
    if (leader.course_id !== lastCourse) {
      lastCourse = leader.course_id;
      const head = document.createElement('div');
      head.className = 'leader-course';
      const swatch = document.createElement('span');
      swatch.className = 'leader-swatch';
      // Bib colour, not line colour: "first yellow male" is what gets said.
      swatch.style.background = leader.bib_color || '#5a6572';
      const name = document.createElement('span');
      name.textContent = leader.bib_color_name
        ? `${leader.course_name} — ${leader.bib_color_name} bibs`
        : leader.course_name;
      head.append(swatch, name);
      host.appendChild(head);
    }

    const row = document.createElement('div');
    row.className = 'leader';

    const line = document.createElement('div');
    line.className = 'leader-main';
    const where = leader.last_poi_name
      ? `${escapeHtml(leader.last_poi_name)} · ${escapeHtml(formatAge(ageSeconds(leader.last_at)))} ago`
      : 'not yet seen';
    const pace = paceLabel(leader.pace_mps);
    line.innerHTML =
      `<span class="leader-div">${escapeHtml(leader.division_label)}</span>` +
      (leader.bib ? `<span class="leader-bib">#${escapeHtml(leader.bib)}</span>` : '') +
      `<span class="leader-at">${where}</span>` +
      (pace ? `<span class="leader-pace">${escapeHtml(pace)}</span>` : '');
    row.appendChild(line);

    if (leader.next_poi_name) {
      const eta = formatEta(leader.eta_seconds);
      const hint = document.createElement('div');
      hint.className = 'leader-next';
      hint.textContent = eta
        ? `Next: ${leader.next_poi_name} (${eta})`
        : `Next: ${leader.next_poi_name}`;
      row.appendChild(hint);
    }

    if (can('leaders')) {
      const controls = document.createElement('div');
      controls.className = 'leader-controls';

      // Prefilled with the last known bib: the leader rarely changes, so this
      // is usually already correct and NCS only edits it when it does.
      const bibField = document.createElement('input');
      bibField.type = 'text';
      bibField.className = 'leader-bib-input';
      bibField.placeholder = 'Bib';
      bibField.maxLength = 16;
      bibField.value = leader.bib || '';
      bibField.setAttribute('aria-label',
        `Bib for ${leader.division_label}, ${leader.course_name}`);
      controls.appendChild(bibField);

      if (leader.next_poi_id) {
        const passed = document.createElement('button');
        passed.type = 'button';
        passed.className = 'leader-passed';
        passed.textContent = `Passed ${leader.next_poi_name}`;
        passed.addEventListener('click',
          () => recordSighting(leader, leader.next_poi_id, bibField.value.trim()));
        controls.appendChild(passed);
      }

      // Correction path: any station, plus undo.
      const picker = document.createElement('select');
      picker.className = 'leader-picker';
      picker.setAttribute('aria-label', `Record ${leader.division_label} at a station`);
      picker.innerHTML = '<option value="">At…</option>' +
        state.aidStations
          .filter((poi) => !leader.course_id || true)
          .map((poi) => `<option value="${poi.id}">${escapeHtml(poi.name)}</option>`)
          .join('');
      picker.addEventListener('change', () => {
        if (picker.value) {
          recordSighting(leader, Number(picker.value), bibField.value.trim());
          picker.value = '';
        }
      });
      controls.appendChild(picker);

      if (leader.last_poi_id) {
        const undo = document.createElement('button');
        undo.type = 'button';
        undo.className = 'leader-undo';
        undo.textContent = 'Undo';
        undo.title = 'Remove the last sighting';
        undo.addEventListener('click', () => undoSighting(leader));
        controls.appendChild(undo);
      }
      row.appendChild(controls);
    }

    host.appendChild(row);
  });
}

/* ---------- incidents ---------------------------------------------------- */

/* An unanswered report outranks everything: the failure this list exists to
   prevent is a pickup sitting undispatched while nobody notices. Within a
   status the longest-waiting comes first. */
// Mirrors incidents.STATUS_RANK on the server. "Dropped off" is delivered and
// done; "closed" also covers a request that ended without a pickup.
const INCIDENT_RANK = {
  reported: 0, en_route: 1, picked_up: 2, dropped_off: 3, closed: 4,
};

function incidentIcon(incident) {
  // Square, so it can never be mistaken for a station (circle/diamond) or an
  // aid station (rounded rect). The bib is the label because that is what gets
  // said on the radio.
  const label = incident.bib ? escapeHtml(incident.bib) : '!';
  return L.divIcon({
    className: '',
    html: `<div class="inc inc--${incident.status}">${label}</div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
  });
}

function incidentPopup(incident) {
  const rows = [['Status', incident.status_label]];
  if (incident.assigned_to) rows.push(['Assigned', incident.assigned_to]);
  if (incident.course_position) {
    rows.push(['Course position',
      `${formatMile(incident.course_position.distance_along_m)} of ` +
      `${incident.course_position.course_name}`]);
  }
  if (incident.note) rows.push(['Note', incident.note]);
  rows.push(['In this status', `${formatAge(ageSeconds(incident.status_at))}`]);
  rows.push(['Reported', `${formatAge(ageSeconds(incident.reported_at))} ago` +
    (incident.reported_by ? ` by ${incident.reported_by}` : '')]);

  const title = incident.bib ? `Bib ${incident.bib}` : 'Pickup (bib unknown)';
  return `<h3>${escapeHtml(title)}</h3><dl>` +
    rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`)
      .join('') + '</dl>';
}

function upsertIncidentMarker(incident) {
  let marker = state.incidentMarkers.get(incident.id);
  if (!marker) {
    marker = L.marker([incident.lat, incident.lon], {icon: incidentIcon(incident)});
    marker.bindPopup(() => incidentPopup(state.incidents.get(incident.id)));
    state.incidentMarkers.set(incident.id, marker);
  } else {
    marker.setLatLng([incident.lat, incident.lon]);
    marker.setIcon(incidentIcon(incident));
  }
  // A closed incident leaves the map but stays in the list, so the map shows
  // only what is still live.
  if (incident.status === 'closed') {
    if (map.hasLayer(marker)) map.removeLayer(marker);
  } else if (!map.hasLayer(marker)) {
    marker.addTo(map);
  }
}

async function setIncidentStatus(id, status) {
  const incident = state.incidents.get(id);
  if (!incident) return;
  const previous = {status: incident.status, status_label: incident.status_label};
  incident.status = status;
  incident.status_label =
    (state.incidentStatuses.find((s) => s.value === status) || {}).label || status;
  renderIncidents();

  try {
    const response = await fetch(
      `/api/${M.slug}/${M.token}/incidents/${id}/status`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status, changed_by: state.operatorInitials}),
      }
    );
    if (!response.ok) throw new Error(String(response.status));
  } catch (err) {
    Object.assign(incident, previous);   // never leave NCS believing a false save
    renderIncidents();
    setLocateStatus('Could not save incident - check the connection');
  }
}

/* Create first, ask questions after. A pickup is called in over the radio
   before anyone has read the bib off the runner, so blocking on a dialog would
   put a modal between NCS and the map at the worst moment. The incident is
   opened immediately and the bib field is filled in when it is known. */
async function createIncident(latlng) {
  try {
    const response = await fetch(`/api/${M.slug}/${M.token}/incidents`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lat: latlng.lat, lon: latlng.lng,
        kind: state.pinKind,
        changed_by: state.operatorInitials,
      }),
    });
    if (!response.ok) throw new Error(String(response.status));
    const created = await response.json();
    setSheet(true);
    // Straight into the field that will be filled in next: the bib for a
    // pickup, the text for a note. A pickup is called in before the bib is
    // known, so this is a convenience and never a requirement.
    window.setTimeout(() => {
      const selector = created.kind === 'note'
        ? `[data-note-for="${created.id}"]` : `[data-bib-for="${created.id}"]`;
      const field = document.querySelector(selector);
      if (field) { field.focus(); field.select(); }
    }, 60);
  } catch (err) {
    setLocateStatus('Could not create the incident');
  }
}

async function editIncident(id, fields) {
  try {
    const response = await fetch(`/api/${M.slug}/${M.token}/incidents/${id}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...fields, changed_by: state.operatorInitials}),
    });
    if (!response.ok) throw new Error(String(response.status));
    return true;
  } catch (err) {
    setLocateStatus('Could not save - check the connection');
    return false;
  }
}

/* Pickups and notes are different problems and are drawn separately.

   A pickup is a dispatch question - is anyone coming - so the queue is ordered
   by urgency and the count means "still waiting". A note is a record for the
   organizer afterwards; nobody is waiting on it, so putting it in that queue
   would make the count lie about how many people are still out there. */
function renderIncidents() {
  const all = [...state.incidents.values()];
  const pickups = all.filter((i) => (i.kind || 'pickup') === 'pickup');
  const notes = all.filter((i) => i.kind === 'note');

  renderPickups(pickups);
  renderNotes(notes);
}

function sortPickups(list) {
  const byNear = state.sortPickupsBy === 'near' && state.mePosition;
  return list.sort((a, b) => {
    // Status always leads, whichever ordering is chosen. Sorting purely by
    // distance would bury a pickup that has been waiting twenty minutes behind
    // one called in a moment ago, and an unanswered report sitting undispatched
    // is the exact failure this list exists to prevent.
    const byStatus = INCIDENT_RANK[a.status] - INCIDENT_RANK[b.status];
    if (byStatus !== 0) return byStatus;
    if (byNear) {
      const da = distanceToMe(a);
      const db = distanceToMe(b);
      if (da != null && db != null && da !== db) return da - db;
    }
    return String(a.status_at).localeCompare(String(b.status_at));
  });
}

function renderPickups(pickups) {
  const host = document.getElementById('incident-list');
  const list = sortPickups(pickups);

  // "Open" means somebody is still waiting: delivered and closed are done.
  const live = list.filter(
    (i) => i.status !== 'closed' && i.status !== 'dropped_off').length;
  document.getElementById('incident-count').textContent = live ? `(${live} waiting)` : '';

  // Nearest is only meaningful once the browser knows where we are, which
  // needs a secure context - so on a club LAN over plain http it stays off,
  // and says why rather than silently doing nothing.
  const nearBtn = document.querySelector('.sort-btn[data-sort="near"]');
  if (nearBtn) {
    nearBtn.disabled = !state.mePosition;
    nearBtn.title = state.mePosition
      ? 'Closest to you first, within each status'
      : 'Needs your location - press the locate button first';
  }
  document.getElementById('pickup-sort').hidden = list.length < 2;

  if (!list.length) {
    host.innerHTML = '<p class="muted">No pickups.</p>';
    return;
  }

  host.innerHTML = '';
  list.forEach((incident) => {
    const row = document.createElement('div');
    row.className = `incident incident--${incident.status}`;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'incident-main';
    const mile = incident.course_position
      ? formatMile(incident.course_position.distance_along_m) : '';
    // Time in the CURRENT status, not since it was reported: "waiting 8
    // minutes with nobody dispatched" is the thing worth seeing.
    // Shown whenever we know it, not only while sorting by it: a driver
    // deciding which to take next wants the number, and hiding it behind the
    // sort would mean the list reorders with nothing on screen explaining why.
    const away = distanceToMe(incident);
    const awayText = away == null ? ''
      : away < 1609.344 ? `${Math.round(away / 0.9144)} yd away`
      : `${(away / 1609.344).toFixed(1)} mi away`;

    main.innerHTML =
      `<span class="inc-dot inc-dot--${incident.status}"></span>` +
      `<span class="name">${escapeHtml(incident.bib ? 'Bib ' + incident.bib : 'Bib unknown')}` +
      (awayText ? `<span class="away">${escapeHtml(awayText)}</span>` : '') +
      `</span>` +
      `<span class="mile">${escapeHtml(mile)}</span>` +
      `<span class="age">${escapeHtml(formatAge(ageSeconds(incident.status_at)))}</span>`;
    main.addEventListener('click', () => {
      const marker = state.incidentMarkers.get(incident.id);
      if (marker && map.hasLayer(marker)) {
        setFollowing(false);
        map.setView(marker.getLatLng(), Math.max(map.getZoom(), 15));
        marker.openPopup();
      }
    });
    row.appendChild(main);

    if (can('incidents')) {
      // Bib and note edited in place. The bib is usually unknown when the
      // incident is opened, and the note stays short by design - see the
      // schema comment on why medical detail is kept out.
      const fields = document.createElement('div');
      fields.className = 'incident-fields';

      const bib = document.createElement('input');
      bib.type = 'text';
      bib.className = 'incident-bib';
      bib.placeholder = 'Bib';
      bib.maxLength = 16;
      bib.value = incident.bib || '';
      bib.setAttribute('aria-label', 'Bib number');
      bib.dataset.bibFor = incident.id;

      const note = document.createElement('input');
      note.type = 'text';
      note.className = 'incident-note';
      note.placeholder = 'Short note';
      note.maxLength = 200;
      note.value = incident.note || '';
      note.setAttribute('aria-label', 'Operational note');

      const commit = (field, name) => {
        const value = field.value.trim();
        if (value === (incident[name] || '')) return;
        incident[name] = value || null;
        editIncident(incident.id, {[name]: value || null});
      };
      bib.addEventListener('change', () => commit(bib, 'bib'));
      note.addEventListener('change', () => commit(note, 'note'));
      [bib, note].forEach((field) => field.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') field.blur();
      }));

      fields.append(bib, note);
      row.appendChild(fields);
    } else if (incident.note) {
      const note = document.createElement('p');
      note.className = 'incident-readonly-note';
      note.textContent = incident.note;
      row.appendChild(note);
    }

    const controls = document.createElement('div');
    controls.className = 'incident-op';
    if (can('incidents')) {
      state.incidentStatuses.forEach((option) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `op-btn${option.value === incident.status ? ' op-btn--on' : ''}`;
        btn.textContent = option.label;
        btn.setAttribute('aria-pressed', String(option.value === incident.status));
        btn.addEventListener('click', () => setIncidentStatus(incident.id, option.value));
        controls.appendChild(btn);
      });
    } else {
      const span = document.createElement('span');
      span.className = 'op-readonly';
      span.textContent = incident.status_label;
      controls.appendChild(span);
    }
    row.appendChild(controls);
    host.appendChild(row);
  });
}

/* Course notes: a blocked intersection, a confusing turn, a marshal who never
   arrived. Newest first, because the recent one is the one being discussed on
   the net; there is no urgency ordering because nobody is waiting.

   Their real audience is the organizer after the event, so the text matters
   more than the workflow and there is no status row at all. */
function renderNotes(notes) {
  const section = document.getElementById('note-section');
  const host = document.getElementById('note-list');
  section.hidden = !notes.length;
  document.getElementById('note-count').textContent =
    notes.length ? `(${notes.length})` : '';
  if (!notes.length) return;

  const list = notes.slice().sort(
    (a, b) => String(b.reported_at).localeCompare(String(a.reported_at)));

  host.innerHTML = '';
  list.forEach((incident) => {
    const row = document.createElement('div');
    row.className = 'incident incident--note';

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'incident-main';
    const mile = incident.course_position
      ? formatMile(incident.course_position.distance_along_m) : '';
    main.innerHTML =
      '<span class="inc-dot inc-dot--note"></span>' +
      `<span class="name">${escapeHtml(incident.note || 'Course note')}</span>` +
      `<span class="mile">${escapeHtml(mile)}</span>` +
      `<span class="age">${escapeHtml(formatAge(ageSeconds(incident.reported_at)))}</span>`;
    main.addEventListener('click', () => {
      const marker = state.incidentMarkers.get(incident.id);
      if (marker && map.hasLayer(marker)) {
        setFollowing(false);
        map.setView(marker.getLatLng(), Math.max(map.getZoom(), 15));
        marker.openPopup();
      }
    });
    row.appendChild(main);

    if (can('incidents')) {
      const field = document.createElement('input');
      field.type = 'text';
      field.className = 'incident-note';
      field.placeholder = 'What happened here';
      field.maxLength = 200;
      field.value = incident.note || '';
      field.setAttribute('aria-label', 'Course note');
      field.dataset.noteFor = incident.id;
      field.addEventListener('change', () => {
        const value = field.value.trim();
        if (value === (incident.note || '')) return;
        incident.note = value || null;
        editIncident(incident.id, {note: value || null});
      });
      field.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') field.blur();
      });
      const fields = document.createElement('div');
      fields.className = 'incident-fields';
      fields.appendChild(field);
      row.appendChild(fields);
    }

    host.appendChild(row);
  });
}

function setDroppingPin(on) {
  state.droppingPin = on;
  document.getElementById('incident-hint').hidden = !on;
  const button = document.getElementById('incident-add');
  button.textContent = on ? 'Cancel'
    : state.pinKind === 'note' ? '+ Drop a course note' : '+ Drop a pickup pin';
  button.classList.toggle('is-active', on);
  document.getElementById('map').style.cursor = on ? 'crosshair' : '';
}

document.querySelectorAll('.sort-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.sortPickupsBy = btn.dataset.sort;
    document.querySelectorAll('.sort-btn').forEach((b) => {
      const on = b === btn;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    savePrefs();
    renderIncidents();
  });
});

document.querySelectorAll('.pin-kind-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.pinKind = btn.dataset.kind;
    document.querySelectorAll('.pin-kind-btn').forEach((b) => {
      const on = b === btn;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    document.getElementById('incident-hint').textContent = state.pinKind === 'note'
      ? 'Tap the map where it happened.'
      : 'Tap the map where the runner is.';
    setDroppingPin(state.droppingPin);
  });
});

document.getElementById('incident-add').addEventListener('click', () => {
  setDroppingPin(!state.droppingPin);
  if (state.droppingPin) setSheet(false);   // get the panel out of the way
});

map.on('click', (ev) => {
  if (!state.droppingPin) return;
  setDroppingPin(false);
  createIncident(ev.latlng);
});

/* ---------- connection -------------------------------------------------- */

function setConnection(kind, text) {
  const el = document.getElementById('conn');
  el.className = `conn conn--${kind}`;
  document.getElementById('conn-text').textContent = text;
}

async function loadState() {
  const response = await fetch(`/api/${M.slug}/${M.token}/state`);
  if (!response.ok) {
    setConnection('down', 'Access denied');
    document.getElementById('event-name').textContent = 'Not available';
    return false;
  }
  const data = await response.json();
  applyState(data);
  return true;
}

function applyState(data) {
  const firstLoad = state.event === null;
  state.event = data.event;
  state.role = data.role;
  state.canWrite = data.can_write;
  state.poiCategories = data.poi_categories || [];
  state.roleLabels = data.role_labels || {};
  state.can = new Set(data.capabilities || (data.can_write ? ['incidents',
    'stations', 'ssid', 'leaders', 'course'] : []));
  state.thresholds = data.thresholds;
  if (Array.isArray(data.op_statuses)) state.opStatuses = data.op_statuses;
  if (Array.isArray(data.incident_statuses)) state.incidentStatuses = data.incident_statuses;
  if (Array.isArray(data.incident_kinds)) state.incidentKinds = data.incident_kinds;
  if (Array.isArray(data.divisions)) state.divisions = data.divisions;
  state.leaders = data.leaders || [];
  state.ssidAlerts = data.ssid_alerts || [];
  // Which places can report a lead runner is the layer's `staffed` flag, not a
  // hardcoded 'aid_station' - the server already answers it that way, and the
  // two disagreeing would mean a staffed Traffic control post could be sighted
  // at on the server and be missing from this list.
  const staffed = new Set(
    (data.poi_categories || []).filter((c) => c.staffed).map((c) => c.key));
  state.aidStations = (data.pois || []).filter((p) => staffed.has(p.poi_type));

  if (firstLoad) {
    const prefs = loadPrefs();
    state.operatorInitials = prefs.initials || '';
    if (prefs.sortPickupsBy) state.sortPickupsBy = prefs.sortPickupsBy;
    state.layerPrefs = Object.assign(defaultLayers(data.role), prefs.layers || {});
    state.visibleCourses = new Set(
      prefs.courses || data.courses.map((c) => c.id)
    );
    showOperatorBox();
    document.getElementById('role-note').textContent =
      data.can_write
        ? `Signed in as ${data.role_label}.`
        : `Signed in as ${data.role_label} — view only.`;
  }

  document.getElementById('event-name').textContent = data.event.name;

  // Keyed by what we actually hear, not by what the roster says: a bare
  // callsign entry bound to WX0MIK-5 must meet its own packets, or it shows as
  // a station that never reports beside an unattributed marker.
  state.roster = new Map(data.roster.map((r) => [r.tracking_key || r.station_key, r]));
  state.positions = new Map(data.positions.map((p) => [p.station_key, p]));

  state.incidentMarkers.forEach((marker) => map.removeLayer(marker));
  state.incidentMarkers.clear();
  state.incidents = new Map((data.incidents || []).map((i) => [i.id, i]));
  state.incidents.forEach((incident) => upsertIncidentMarker(incident));

  drawCourses(data.courses);
  drawPois(data.pois);

  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers.clear();
  state.positions.forEach((_, stationKey) => upsertStationMarker(stationKey));

  renderCourseToggles(data.courses);
  if (firstLoad) renderLayerToggles();
  document.getElementById('incident-add').hidden = !can('incidents');
  document.getElementById('pin-kind').hidden = !can('incidents');
  renderSsidAlerts();
  renderLeaders();
  renderIncidents();
  renderStations();
  if (firstLoad) fitToContent();
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws/${M.slug}/${M.token}`);
  state.socket = socket;

  socket.addEventListener('open', () => {
    state.reconnectDelay = 1000;
    setConnection('live', 'Live');
  });

  socket.addEventListener('message', (ev) => {
    let message;
    try {
      message = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    if (message.type === 'resync') {
      loadState();
      return;
    }
    if (message.type === 'leaders') {
      state.leaders = message.leaders || [];
      renderLeaders();
      return;
    }
    if (message.type === 'incident') {
      state.incidents.set(message.id, message);
      upsertIncidentMarker(message);
      renderIncidents();
      return;
    }
    if (message.type === 'station_status') {
      const entry = state.roster.get(message.station_key);
      if (entry) {
        entry.op_status = message.op_status;
        entry.op_status_at = message.op_status_at;
        entry.op_status_by = message.op_status_by;
        entry.op_status_label = message.op_status_label;
        renderStations();
      }
      return;
    }
    if (message.type === 'position') {
      state.positions.set(message.station_key, message);
      upsertStationMarker(message.station_key);
      renderStations();
    }
  });

  socket.addEventListener('close', () => {
    setConnection('down', 'Reconnecting…');
    scheduleReconnect();
  });

  socket.addEventListener('error', () => socket.close());
}

function scheduleReconnect() {
  const delay = Math.min(state.reconnectDelay, 30000);
  setTimeout(async () => {
    setConnection('connecting', 'Connecting…');
    // Full resync, not a replay: a phone back from a dead zone must not show a
    // stale picture as if it were current.
    await loadState();
    connect();
  }, delay);
  state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
}

/* Ages are relative, so redraw on a timer even when no packet arrives —
   otherwise "2m ago" would sit there reading 2m forever. */
setInterval(() => {
  renderStations();
  renderIncidents();
  renderLeaders();
  state.markers.forEach((_, stationKey) => {
    const marker = state.markers.get(stationKey);
    if (marker) marker.setIcon(stationIcon(stationKey));
  });
}, 15000);

/* ---------- geolocation ------------------------------------------------- */

/* The viewer's own position, from the browser. Entirely local: it is never
   sent to the server, never stored, and no other viewer can see it. Logistics
   and Shadow are moving around the course, so this TRACKS rather than taking a
   single fix - a dot frozen where you tapped five minutes ago is worse than
   no dot, because it looks current. */

function setFollowing(on) {
  state.following = on;
  document.getElementById('locate-btn').setAttribute('aria-pressed', String(on));
}

function setLocateStatus(text) {
  // Deliberately NOT the connection badge: that reports the data feed, and
  // masking it with a location problem would hide a stale map.
  const el = document.getElementById('locate-status');
  el.textContent = text || '';
  el.hidden = !text;
}

function stopWatching() {
  if (state.geoWatchId != null) {
    navigator.geolocation.clearWatch(state.geoWatchId);
    state.geoWatchId = null;
  }
  setFollowing(false);
  state.mePosition = null;
  if (state.sortPickupsBy === 'near') renderIncidents();
  if (state.meMarker) { map.removeLayer(state.meMarker); state.meMarker = null; }
  if (state.meAccuracy) { map.removeLayer(state.meAccuracy); state.meAccuracy = null; }
  setLocateStatus('');
}

function onPosition(pos) {
  const latlng = [pos.coords.latitude, pos.coords.longitude];
  const accuracy = pos.coords.accuracy;

  // Kept so the pickup queue can be ordered by how near each one is. Still
  // local: this never leaves the browser, exactly as the dot does not.
  state.mePosition = {lat: latlng[0], lon: latlng[1]};
  if (state.sortPickupsBy === 'near') renderIncidents();

  if (!state.meMarker) {
    // Accuracy circle first so the dot draws on top of it.
    state.meAccuracy = L.circle(latlng, {
      radius: accuracy, color: '#0b5fa5', weight: 1,
      fillColor: '#0b5fa5', fillOpacity: 0.12, interactive: false,
    }).addTo(map);
    state.meMarker = L.circleMarker(latlng, {
      radius: 8, color: '#ffffff', weight: 3,
      fillColor: '#0b5fa5', fillOpacity: 1,
    }).addTo(map).bindPopup('You are here');
    map.setView(latlng, Math.max(map.getZoom(), 15));
  } else {
    state.meMarker.setLatLng(latlng);
    state.meAccuracy.setLatLng(latlng).setRadius(accuracy);
    if (state.following) map.panTo(latlng);
  }

  // A 500 m "fix" is wifi triangulation, not GPS. Say so rather than letting
  // someone trust a dot that could be anywhere in the neighbourhood.
  setLocateStatus(accuracy > 100 ? `Location approximate (±${Math.round(accuracy)} m)` : '');
}

function onPositionError(err) {
  if (err.code === err.PERMISSION_DENIED) {
    setLocateStatus('Location permission denied');
  } else if (err.code === err.TIMEOUT) {
    setLocateStatus('No GPS fix yet');
  } else {
    setLocateStatus('Location unavailable');
  }
  setFollowing(false);
}

document.getElementById('locate-btn').addEventListener('click', () => {
  if (state.geoWatchId != null) {           // second tap turns it off
    stopWatching();
    return;
  }
  if (!navigator.geolocation) {
    setLocateStatus('This browser has no location support');
    return;
  }
  // Browsers block geolocation outside a secure context. On a club LAN served
  // over plain http:// this fails with a bare permission error that looks like
  // the user's fault, so name the real cause.
  if (!window.isSecureContext) {
    setLocateStatus('Location needs HTTPS (or localhost)');
    return;
  }

  setLocateStatus('Locating…');
  setFollowing(true);
  state.geoWatchId = navigator.geolocation.watchPosition(
    onPosition, onPositionError,
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
  );
});

/* ---------- operator initials ------------------------------------------- */

/* Typed once per shift and kept in this browser. It annotates who changed a
   status so a handover can see what happened; it is NOT authentication and
   nothing should ever start trusting it as identity. */
const initialsInput = document.getElementById('operator-initials');

initialsInput.addEventListener('input', () => {
  state.operatorInitials = initialsInput.value.trim().toUpperCase().slice(0, 12);
  savePrefs();
});

function showOperatorBox() {
  document.getElementById('operator-box').hidden = !state.canWrite;
  initialsInput.value = state.operatorInitials || '';
}

/* ---------- sheet ------------------------------------------------------- */

const sheet = document.getElementById('sheet');
const sheetToggle = document.getElementById('sheet-toggle');

function setSheet(open) {
  sheet.classList.toggle('open', open);
  sheetToggle.setAttribute('aria-expanded', String(open));
  document.getElementById('sheet-toggle-label').textContent = open ? 'Hide' : 'Layers';
}

sheetToggle.addEventListener('click', () => setSheet(!sheet.classList.contains('open')));
document.getElementById('sheet-grip').addEventListener('click', () => setSheet(false));

/* ---------- go ---------------------------------------------------------- */

(async () => {
  if (await loadState()) connect();
})();
