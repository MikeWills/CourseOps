/* AprsWebTracker map client.
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
  thresholds: { stale_after_s: 600, silent_after_s: 1200 },
  roster: new Map(),          // station_key -> roster row
  positions: new Map(),       // station_key -> latest position
  markers: new Map(),         // station_key -> L.Marker
  courseLayers: new Map(),    // course id -> L.Polyline
  poiLayer: null,
  visibleCourses: new Set(),
  layerPrefs: {},
  socket: null,
  reconnectDelay: 1000,
  opStatuses: ['pending', 'active', 'closed'],
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
    pois: true,
  };
}

const LAYER_LABELS = {
  aid_station_ops: 'Aid station operators',
  sweep: 'Sweeps',
  sag: 'SAG',
  shadow: 'Shadows',
  rover: 'Rovers',
  net_control: 'Net control',
  start_finish: 'Start / finish stations',
  pois: 'Aid station locations',
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

function labelOf(stationKey) {
  const entry = state.roster.get(stationKey);
  return entry ? entry.display_label : stationKey;
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

function drawPois(pois) {
  if (state.poiLayer) map.removeLayer(state.poiLayer);
  state.poiLayer = L.layerGroup();

  pois.forEach((poi) => {
    const marker = L.marker([poi.lat, poi.lon], {
      icon: L.divIcon({
        className: '',
        html: `<div class="poi-icon">${escapeHtml(initials(poi.name))}</div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      }),
      title: poi.name,
    });
    const rows = [['Type', poi.poi_type.replace(/_/g, ' ')]];
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
    state.poiLayer.addLayer(marker);
  });

  if (state.layerPrefs.pois !== false) state.poiLayer.addTo(map);
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

function renderLayerToggles() {
  const host = document.getElementById('layer-list');
  host.innerHTML = '';
  Object.keys(LAYER_LABELS).forEach((key) => {
    const row = document.createElement('label');
    row.className = 'toggle';
    row.innerHTML =
      `<input type="checkbox" ${state.layerPrefs[key] !== false ? 'checked' : ''}>` +
      `<span class="label">${escapeHtml(LAYER_LABELS[key])}</span>`;
    row.querySelector('input').addEventListener('change', (ev) => {
      state.layerPrefs[key] = ev.target.checked;
      applyLayerVisibility();
      savePrefs();
    });
    host.appendChild(row);
  });
}

function applyLayerVisibility() {
  state.markers.forEach((_, stationKey) => upsertStationMarker(stationKey));
  if (state.poiLayer) {
    if (state.layerPrefs.pois !== false) {
      if (!map.hasLayer(state.poiLayer)) state.poiLayer.addTo(map);
    } else if (map.hasLayer(state.poiLayer)) {
      map.removeLayer(state.poiLayer);
    }
  }
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
    locate.innerHTML =
      `<span class="dot dot--${status}"></span>` +
      `<span class="name">${escapeHtml(labelOf(stationKey))}</span>` +
      `<span class="call">${escapeHtml(stationKey)}</span>` +
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
    if (state.canWrite) {
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
  state.thresholds = data.thresholds;
  if (Array.isArray(data.op_statuses)) state.opStatuses = data.op_statuses;

  if (firstLoad) {
    const prefs = loadPrefs();
    state.operatorInitials = prefs.initials || '';
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

  state.roster = new Map(data.roster.map((r) => [r.station_key, r]));
  state.positions = new Map(data.positions.map((p) => [p.station_key, p]));

  drawCourses(data.courses);
  drawPois(data.pois);

  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers.clear();
  state.positions.forEach((_, stationKey) => upsertStationMarker(stationKey));

  renderCourseToggles(data.courses);
  if (firstLoad) renderLayerToggles();
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
  if (state.meMarker) { map.removeLayer(state.meMarker); state.meMarker = null; }
  if (state.meAccuracy) { map.removeLayer(state.meAccuracy); state.meAccuracy = null; }
  setLocateStatus('');
}

function onPosition(pos) {
  const latlng = [pos.coords.latitude, pos.coords.longitude];
  const accuracy = pos.coords.accuracy;

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
