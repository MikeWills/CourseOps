/* Course Ops setup.
 *
 * Everything a club needs to configure an event, so nobody has to open a
 * terminal. The two things that cannot live here are the callsign in .env and
 * starting the server, because both happen before this page exists.
 *
 * The course review is the part that most needed a screen rather than a
 * command: organizer files are wrong in ways a list of names cannot show - a
 * course split into five pieces, a stray line miles away, a folder mixing water
 * stops with parking. Seeing them on a map is what makes the decision obvious.
 */
'use strict';

const S = {
  user: null,
  events: [],
  eventId: null,
  staged: [],
  picked: new Set(),
  map: null,
  layers: new Map(),
  roster: [],
  categories: [],
  pois: [],
  editing: null,
  organizations: [],
};

const $ = (id) => document.getElementById(id);

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function miles(m) { return m == null ? '' : (m / 1609.344).toFixed(1) + ' mi'; }

function banner(message, isError) {
  const el = $('banner');
  el.textContent = message;
  el.classList.toggle('is-error', !!isError);
  el.hidden = !message;
  if (message && !isError) setTimeout(() => { el.hidden = true; }, 4000);
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({
    headers: {'Content-Type': 'application/json'},
  }, options || {}));
  if (response.status === 401) { showGate(false); throw new Error('Sign in again.'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Error ${response.status}`);
  return data;
}

const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});

/* ---------- sign in ------------------------------------------------------ */

function showGate(firstRun) {
  $('gate').hidden = false;
  $('app').hidden = true;
  $('whoami').hidden = true;
  $('logout').hidden = true;
  $('gate-heading').textContent = firstRun ? 'Create your account' : 'Sign in';
  $('gate-intro').textContent = firstRun
    ? 'Nobody has an account yet. This first one is a system administrator, and '
      + 'this form closes as soon as it exists.'
    : '';
  $('gate-display-field').hidden = !firstRun;
  $('gate-submit').textContent = firstRun ? 'Create account' : 'Sign in';
  $('gate-password').autocomplete = firstRun ? 'new-password' : 'current-password';
  $('gate-form').dataset.firstRun = firstRun ? 'true' : 'false';
}

$('gate-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const firstRun = $('gate-form').dataset.firstRun === 'true';
  $('gate-error').hidden = true;
  try {
    const body = {
      username: $('gate-username').value,
      password: $('gate-password').value,
    };
    if (firstRun) body.display_name = $('gate-display').value;
    const data = await post(firstRun ? '/api/setup/first-user' : '/api/setup/login', body);
    S.user = data.user;
    await start();
  } catch (err) {
    $('gate-error').textContent = err.message;
    $('gate-error').hidden = false;
  }
});

$('logout').addEventListener('click', async () => {
  await post('/api/setup/logout');
  location.reload();
});

/* ---------- tabs --------------------------------------------------------- */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('is-on', t === tab));
    document.querySelectorAll('.panel').forEach((p) => {
      p.hidden = p.dataset.panel !== tab.dataset.tab;
    });
    refreshTab(tab.dataset.tab);
  });
});

function needEvent() {
  if (S.eventId) return true;
  banner('Pick an event first.', true);
  return false;
}

async function refreshTab(name) {
  try {
    if (name === 'orgs') return loadOrgs();
    if (name === 'events') return loadEvents();
    if (name === 'users') return loadUsers();
    if (!needEvent()) return;
    if (name === 'course') return loadStaged();
    if (name === 'stations') return loadCourses();
    if (name === 'roster') return loadRoster();
    if (name === 'links') return loadLinks();
  } catch (err) { banner(err.message, true); }
}

/* ---------- organizations ------------------------------------------------ */

/* The tenancy boundary. Only the host adds clubs; a club officer works inside
   theirs and never sees another. */

async function loadOrgs() {
  const data = await api('/api/setup/organizations');
  S.organizations = data.organizations;
  $('org-form').hidden = !S.user.is_system_admin;

  $('org-list').innerHTML = S.organizations.length ? `
    <table class="grid"><thead><tr><th>Organization</th><th>Events</th>
      <th>Admins</th><th>Contact</th><th></th></tr></thead><tbody>` +
    S.organizations.map((o) => `<tr>
      <td><strong>${esc(o.name)}</strong><br>
        <span class="muted">${esc(o.slug)}</span></td>
      <td>${o.event_count}</td><td>${o.admin_count}</td>
      <td>${esc(o.contact || '')}</td>
      <td class="actions">${S.user.is_system_admin
        ? `<button class="danger" data-delo="${o.id}">Delete</button>` : ''}</td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">No organizations yet.</p>';

  $('org-list').querySelectorAll('[data-delo]').forEach((b) =>
    b.addEventListener('click', async () => {
      const org = S.organizations.find((o) => o.id === Number(b.dataset.delo));
      // Cascades through every event the club owns, and everything recorded
      // for them, so the confirmation names the damage.
      if (!confirm(`Delete "${org.name}"?\n\nThis also deletes its `
        + `${org.event_count} event(s), all their history, and `
        + `${org.admin_count} administrator account(s). It cannot be undone.`)) return;
      try {
        await post(`/api/setup/organizations/${org.id}/delete`);
        banner(`Deleted ${org.name}.`);
        loadOrgs();
      } catch (err) { banner(err.message, true); }
    }));
}

$('org-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('org-error').hidden = true;
  try {
    const created = await post('/api/setup/organizations', {
      slug: $('og-slug').value, name: $('og-name').value,
      contact: $('og-contact').value,
    });
    $('org-form').reset();
    banner(`Created ${created.name}.`);
    loadOrgs();
  } catch (err) {
    $('org-error').textContent = err.message;
    $('org-error').hidden = false;
  }
});

function fillOrgSelect(select) {
  select.innerHTML = S.organizations
    .map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
}

/* ---------- events ------------------------------------------------------- */

async function loadEvents() {
  const data = await api('/api/setup/events');
  S.events = data.events;
  if (data.organizations && data.organizations.length) {
    S.organizations = data.organizations;
  }
  // An org admin creates events too, but always inside their own club, so the
  // picker only appears for the host.
  $('event-form').hidden = !S.user.may_create_events;
  $('ev-org-field').hidden = !S.user.is_system_admin;
  if (S.user.is_system_admin) fillOrgSelect($('ev-org'));

  const host = $('event-list');
  if (!S.events.length) {
    host.innerHTML = '<p class="muted">No events yet.'
      + (S.user.is_system_admin ? ' Create one below.' : '') + '</p>';
    return;
  }
  host.innerHTML = '<table class="grid"><thead><tr><th>Event</th><th>Date</th>'
    + '<th>Course</th><th>Aid</th><th>Roster</th><th></th></tr></thead><tbody>'
    + S.events.map((e) => `<tr${e.id === S.eventId ? ' style="background:#eaf2fb"' : ''}>
        <td><strong>${esc(e.name)}</strong><br><span class="muted">${esc(e.slug)}</span></td>
        <td>${esc(e.event_date || '')}</td>
        <td>${e.counts.courses}</td>
        <td>${e.counts.pois}</td>
        <td>${e.counts.roster}</td>
        <td class="actions">
          <button data-pick="${e.id}">${e.id === S.eventId ? 'Selected' : 'Select'}</button>
          ${S.user.is_system_admin
            ? `<button class="danger" data-del="${e.id}">Delete</button>` : ''}
        </td></tr>`).join('') + '</tbody></table>';

  host.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
    selectEvent(Number(b.dataset.pick));
  }));
  host.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    const event = S.events.find((e) => e.id === Number(b.dataset.del));
    // Deleting cascades through positions, incidents and the whole history, so
    // the confirmation names what is being destroyed.
    if (!confirm(`Delete "${event.name}" and everything recorded for it?\n\n`
      + `This removes ${event.counts.courses} course(s), ${event.counts.pois} aid `
      + `station(s), ${event.counts.roster} roster entries and all position `
      + `history. It cannot be undone.`)) return;
    try {
      await post(`/api/setup/events/${event.id}/delete`);
      if (S.eventId === event.id) S.eventId = null;
      banner(`Deleted ${event.name}.`);
      loadEvents();
    } catch (err) { banner(err.message, true); }
  }));
}

function selectEvent(id) {
  S.eventId = id;
  const event = S.events.find((e) => e.id === id);
  $('event-context').textContent = event ? `Working on: ${event.name}` : '';
  $('event-context').hidden = !event;
  loadEvents();
}

$('event-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('event-error').hidden = true;
  try {
    const created = await post('/api/setup/events', {
      slug: $('ev-slug').value,
      name: $('ev-name').value,
      event_date: $('ev-date').value,
      timezone: $('ev-tz').value,
      organization_id: S.user.is_system_admin
        ? Number($('ev-org').value) : undefined,
    });
    $('ev-slug').value = ''; $('ev-name').value = '';
    banner(`Created ${created.name}.`);
    await loadEvents();
    selectEvent(created.id);
  } catch (err) {
    $('event-error').textContent = err.message;
    $('event-error').hidden = false;
  }
});

/* ---------- course import and review ------------------------------------- */

$('course-file').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file || !needEvent()) return;
  $('import-status').hidden = false;
  $('import-status').textContent = `Reading ${file.name}…`;

  const form = new FormData();
  form.append('file', file);
  try {
    const response = await fetch(`/api/setup/events/${S.eventId}/import`,
      {method: 'POST', body: form});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Import failed');
    const kinds = Object.entries(data.by_type)
      .map(([k, n]) => `${n} ${k}`).join(', ');
    $('import-status').textContent =
      `${data.filename}: ${data.total} features (${kinds}).`
      + (data.warnings.length ? ` ${data.warnings.length} warning(s).` : '');
    renderReview(data.features);
  } catch (err) {
    $('import-status').textContent = err.message;
    banner(err.message, true);
  }
  ev.target.value = '';
});

async function loadStaged() {
  const data = await api(`/api/setup/events/${S.eventId}/staged`);
  renderReview(data.features);
}

function ensureMap() {
  if (S.map) return S.map;
  S.map = L.map('review-map');
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(S.map);
  S.map.setView([39.5, -98.35], 4);
  return S.map;
}

function renderReview(features) {
  S.staged = features || [];
  S.picked.clear();
  $('review').hidden = S.staged.length === 0;
  $('review-actions').hidden = true;
  if (!S.staged.length) return;

  $('review-count').textContent = `(${S.staged.length} awaiting a decision)`;
  const map = ensureMap();
  S.layers.forEach((l) => map.removeLayer(l));
  S.layers.clear();

  const bounds = L.latLngBounds([]);
  S.staged.forEach((f) => {
    const layer = f.geom_type === 'point'
      ? L.circleMarker([f.geojson.coordinates[1], f.geojson.coordinates[0]],
          {radius: 7, color: '#0B2545', fillColor: '#FF6A13', fillOpacity: 1, weight: 2})
      : L.polyline(f.geojson.coordinates.map(([lon, lat]) => [lat, lon]),
          {color: '#0B2545', weight: 4, opacity: 0.8});
    layer.bindTooltip(f.name);
    layer.on('click', () => togglePick(f.id));
    layer.addTo(map);
    S.layers.set(f.id, layer);
    bounds.extend(layer.getBounds ? layer.getBounds() : layer.getLatLng());
  });
  if (bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24]});
  setTimeout(() => map.invalidateSize(), 60);

  $('review-list').innerHTML = S.staged.map((f) => `
    <label class="feature" data-id="${f.id}">
      <input type="checkbox">
      <span class="what"><strong>${esc(f.name)}</strong>
        ${f.folder ? `<span class="where"><br>${esc(f.folder)}</span>` : ''}</span>
      <span class="hint">${esc(f.geom_type)}${f.length_m ? ' · ' + miles(f.length_m) : ''}
        ${f.suggestion && f.suggestion !== 'unassigned'
          ? ' · looks like ' + esc(f.suggestion.replace('poi:', '')) : ''}</span>
    </label>`).join('');

  $('review-list').querySelectorAll('.feature').forEach((row) => {
    row.querySelector('input').addEventListener('change', () =>
      togglePick(Number(row.dataset.id)));
  });
}

function togglePick(id) {
  if (S.picked.has(id)) S.picked.delete(id); else S.picked.add(id);
  S.staged.forEach((f) => {
    const on = S.picked.has(f.id);
    const row = $('review-list').querySelector(`[data-id="${f.id}"]`);
    if (row) { row.classList.toggle('is-picked', on); row.querySelector('input').checked = on; }
    const layer = S.layers.get(f.id);
    // The map and the list always agree about what is about to be assigned.
    if (layer && layer.setStyle) {
      layer.setStyle(on ? {color: '#FF6A13', weight: 6} : {color: '#0B2545', weight: 4});
    }
  });
  $('review-actions').hidden = S.picked.size === 0;

  const picked = S.staged.filter((f) => S.picked.has(f.id));
  const allLines = picked.length > 0 && picked.every((f) => f.geom_type !== 'point');
  $('assign-type').value = allLines ? 'course' : 'aid_station';
  if (picked.length === 1 && !$('assign-name').value) {
    $('assign-name').value = picked[0].name.replace(/\s*\[\d+\]$/, '');
  }
}

$('assign-go').addEventListener('click', async () => {
  const type = $('assign-type').value;
  const ids = [...S.picked];
  try {
    const body = type === 'course'
      ? {kind: 'course', ids, name: $('assign-name').value,
         reverse: $('assign-reverse').checked}
      : {kind: 'poi', ids, poi_type: type, name: $('assign-name').value};
    const result = await post(`/api/setup/events/${S.eventId}/assign`, body);
    if (result.warnings && result.warnings.length) {
      banner(result.warnings.join(' '), true);
    } else {
      banner(type === 'course'
        ? `Course created (${miles(result.distance_m)}).`
        : `${ids.length} aid station(s) created.`);
    }
    $('assign-name').value = '';
    $('assign-reverse').checked = false;
    loadStaged();
  } catch (err) { banner(err.message, true); }
});

$('assign-discard').addEventListener('click', async () => {
  try {
    await post(`/api/setup/events/${S.eventId}/assign`,
      {kind: 'discard', ids: [...S.picked]});
    banner('Discarded.');
    loadStaged();
  } catch (err) { banner(err.message, true); }
});

/* ---------- courses and aid stations ------------------------------------- */

async function loadCourses() {
  const data = await api(`/api/setup/events/${S.eventId}/courses`);
  S.pois = data.pois;

  $('course-table').innerHTML = data.courses.length ? `
    <table class="grid"><thead><tr><th>Course</th><th>Distance</th><th>Line</th>
      <th>Bib colour</th><th></th></tr></thead><tbody>` +
    data.courses.map((c) => `<tr>
      <td><input value="${esc(c.name)}" data-name="${c.id}" style="width:130px"></td>
      <td>${miles(c.distance_m)}</td>
      <td><input type="color" value="${esc(c.color || '#d55e00')}" data-color="${c.id}"></td>
      <td><input type="color" value="${esc(c.bib_color || c.color || '#d55e00')}"
            data-bib="${c.id}">
          <input placeholder="Yellow" value="${esc(c.bib_color_name || '')}"
            data-bibname="${c.id}" style="width:90px"></td>
      <td class="actions"><button data-savec="${c.id}">Save</button>
        <button class="danger" data-delc="${c.id}">Delete</button></td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">No courses yet — import a KML on the Course tab.</p>';

  $('course-table').querySelectorAll('[data-savec]').forEach((b) =>
    b.addEventListener('click', async () => {
      const id = b.dataset.savec;
      try {
        await post(`/api/setup/events/${S.eventId}/courses/${id}`, {
          name: $('course-table').querySelector(`[data-name="${id}"]`).value,
          color: $('course-table').querySelector(`[data-color="${id}"]`).value,
          bib_color: $('course-table').querySelector(`[data-bib="${id}"]`).value,
          bib_color_name: $('course-table').querySelector(`[data-bibname="${id}"]`).value,
        });
        banner('Course saved.');
        loadCourses();
      } catch (err) { banner(err.message, true); }
    }));
  $('course-table').querySelectorAll('[data-delc]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('Delete this course?')) return;
      await post(`/api/setup/events/${S.eventId}/courses/${b.dataset.delc}/delete`);
      loadCourses();
    }));

  $('poi-table').innerHTML = data.pois.length ? `
    <table class="grid"><thead><tr><th>Mile</th><th>Name</th><th>Type</th>
      <th>What3Words</th><th></th></tr></thead><tbody>` +
    data.pois.map((p) => `<tr>
      <td>${p.distance_along_m != null ? miles(p.distance_along_m) : '—'}</td>
      <td><input value="${esc(p.name)}" data-pname="${p.id}" style="width:150px"></td>
      <td>${esc(p.poi_type.replace(/_/g, ' '))}</td>
      <td><input value="${esc(p.what3words || '')}" data-w3w="${p.id}"
            placeholder="filled.count.soap" style="width:170px"></td>
      <td class="actions"><button data-savep="${p.id}">Save</button>
        <button class="danger" data-delp="${p.id}">Delete</button></td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">No aid stations yet.</p>';

  $('poi-table').querySelectorAll('[data-savep]').forEach((b) =>
    b.addEventListener('click', async () => {
      const id = b.dataset.savep;
      try {
        await post(`/api/setup/events/${S.eventId}/pois/${id}`, {
          name: $('poi-table').querySelector(`[data-pname="${id}"]`).value,
          what3words: $('poi-table').querySelector(`[data-w3w="${id}"]`).value,
        });
        banner('Aid station saved.');
        loadCourses();
      } catch (err) { banner(err.message, true); }
    }));
  $('poi-table').querySelectorAll('[data-delp]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('Delete this aid station?')) return;
      await post(`/api/setup/events/${S.eventId}/pois/${b.dataset.delp}/delete`);
      loadCourses();
    }));
}

/* ---------- roster ------------------------------------------------------- */

const CATEGORY_LABELS = {
  net_control: 'Net control', aid_station: 'Aid station', sweep: 'Sweep',
  sag: 'SAG', shadow: 'Shadow', rover: 'Rover', start_finish: 'Start / finish',
};

async function loadRoster() {
  const data = await api(`/api/setup/events/${S.eventId}/roster`);
  S.roster = data.roster;
  S.categories = data.categories;
  S.pois = data.pois;

  $('rs-category').innerHTML = data.categories
    .map((c) => `<option value="${esc(c)}">${esc(CATEGORY_LABELS[c] || c)}</option>`).join('');
  $('rs-poi').innerHTML = '<option value="">Not posted</option>' +
    data.pois.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('');

  $('roster-table').innerHTML = S.roster.length ? `
    <table class="grid"><thead><tr><th>Callsign</th><th>Label</th><th>Role</th>
      <th>APRS</th><th>Posted at</th><th></th></tr></thead><tbody>` +
    S.roster.map((r) => `<tr>
      <td><strong>${esc(r.station_key)}</strong></td>
      <td>${esc(r.display_label)}</td>
      <td>${esc(CATEGORY_LABELS[r.category] || r.category)}</td>
      <td>${r.expects_aprs
        ? '<span class="pill">tracked</span>'
        : '<span class="pill is-off">no APRS</span>'}</td>
      <td>${esc(r.poi_name || '')}</td>
      <td class="actions"><button data-edit="${esc(r.station_key)}">Edit</button>
        <button class="danger" data-delr="${esc(r.station_key)}">Remove</button></td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">Nobody on the roster yet.</p>';

  $('roster-table').querySelectorAll('[data-edit]').forEach((b) =>
    b.addEventListener('click', () => editRoster(b.dataset.edit)));
  $('roster-table').querySelectorAll('[data-delr]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm(`Remove ${b.dataset.delr} from the roster?`)) return;
      await post(`/api/setup/events/${S.eventId}/roster/delete`,
        {station_key: b.dataset.delr});
      loadRoster();
    }));
}

function editRoster(key) {
  const entry = S.roster.find((r) => r.station_key === key);
  if (!entry) return;
  S.editing = key;
  $('roster-form-title').textContent = `Edit ${key}`;
  $('rs-call').value = entry.station_key;
  $('rs-label').value = entry.display_label;
  $('rs-category').value = entry.category;
  $('rs-operator').value = entry.operator_name || '';
  $('rs-poi').value = entry.poi_id || '';
  $('rs-aprs').checked = !!entry.expects_aprs;
  $('roster-cancel').hidden = false;
  $('rs-call').focus();
}

$('roster-cancel').addEventListener('click', () => resetRosterForm());

function resetRosterForm() {
  S.editing = null;
  $('roster-form').reset();
  $('rs-aprs').checked = true;
  $('roster-form-title').textContent = 'Add a station';
  $('roster-cancel').hidden = true;
}

$('roster-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('roster-error').hidden = true;
  try {
    await post(`/api/setup/events/${S.eventId}/roster`, {
      station_key: $('rs-call').value,
      original_station_key: S.editing || '',
      display_label: $('rs-label').value,
      category: $('rs-category').value,
      operator_name: $('rs-operator').value,
      expects_aprs: $('rs-aprs').checked,
      poi_id: $('rs-poi').value ? Number($('rs-poi').value) : null,
    });
    banner('Station saved.');
    resetRosterForm();
    loadRoster();
  } catch (err) {
    $('roster-error').textContent = err.message;
    $('roster-error').hidden = false;
  }
});

/* ---------- links -------------------------------------------------------- */

async function loadLinks() {
  const data = await api(`/api/setup/events/${S.eventId}/links`);
  const live = data.links.filter((l) => !l.revoked);
  $('link-list').innerHTML = live.map((l) => {
    const url = `${location.origin}/e/${data.slug}/${l.token}`;
    return `<div class="link-row">
      <div class="link-role">${esc(l.role_label)}</div>
      <input class="link-url" readonly value="${esc(url)}">
      <div class="link-actions">
        <button data-copy="${esc(url)}">Copy</button>
        <button class="danger" data-reissue="${esc(l.role)}">Revoke &amp; reissue</button>
      </div>
      <p class="muted">${l.last_used ? 'Last used ' + esc(l.last_used) : 'Never used'}</p>
    </div>`;
  }).join('');

  $('link-list').querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.copy);
        banner('Link copied.');
      } catch (err) {
        banner('Could not copy — select the text and copy it manually.', true);
      }
    }));
  $('link-list').querySelectorAll('[data-reissue]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('Anyone using the current link will lose access immediately. '
        + 'Continue?')) return;
      await post(`/api/setup/events/${S.eventId}/links`,
        {action: 'reissue', role: b.dataset.reissue});
      banner('New link issued — send it to that group.');
      loadLinks();
    }));
}

/* ---------- users -------------------------------------------------------- */

async function loadUsers() {
  const data = await api('/api/setup/users');
  if (data.organizations && data.organizations.length) {
    S.organizations = data.organizations;
  }
  $('us-org-field').hidden = !S.user.is_system_admin;
  if (S.user.is_system_admin) fillOrgSelect($('us-org'));
  $('us-role').innerHTML = data.roles
    .map((r) => `<option value="${esc(r.value)}">${esc(r.label)}</option>`).join('');
  renderUserEvents();

  $('user-list').innerHTML = `<table class="grid"><thead><tr><th>User</th>
    <th>Role</th><th>Events</th><th>Status</th><th></th></tr></thead><tbody>` +
    data.users.map((u) => `<tr>
      <td><strong>${esc(u.username)}</strong>${u.display_name
        ? `<br><span class="muted">${esc(u.display_name)}</span>` : ''}</td>
      <td>${esc(u.role_label)}</td>
      <td>${u.is_system_admin ? '<span class="muted">all</span>'
        : (u.is_org_admin ? '<span class="muted">whole club</span>' : u.events.length)}</td>
      <td>${u.is_active ? '<span class="pill">active</span>'
        : '<span class="pill is-off">disabled</span>'}</td>
      <td class="actions">
        <button data-pw="${u.id}">Password</button>
        <button data-toggle="${u.id}" data-active="${u.is_active ? '1' : '0'}">
          ${u.is_active ? 'Disable' : 'Enable'}</button>
        <button class="danger" data-delu="${u.id}">Delete</button>
      </td></tr>`).join('') + '</tbody></table>';

  $('user-list').querySelectorAll('[data-pw]').forEach((b) =>
    b.addEventListener('click', async () => {
      const password = prompt('New password (at least 10 characters):');
      if (!password) return;
      try {
        await post(`/api/setup/users/${b.dataset.pw}`, {password});
        banner('Password changed. Their existing sessions were signed out.');
      } catch (err) { banner(err.message, true); }
    }));
  $('user-list').querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        await post(`/api/setup/users/${b.dataset.toggle}`,
          {is_active: b.dataset.active !== '1'});
        loadUsers();
      } catch (err) { banner(err.message, true); }
    }));
  $('user-list').querySelectorAll('[data-delu]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('Delete this administrator?')) return;
      try {
        await post(`/api/setup/users/${b.dataset.delu}/delete`);
        loadUsers();
      } catch (err) { banner(err.message, true); }
    }));
}

function renderUserEvents() {
  const role = $('us-role').value;
  // Only an event admin needs a list: system and org admins are scoped by the
  // system and by their club respectively.
  $('us-events-field').hidden = role !== 'event_admin';
  $('us-org-field').hidden = !S.user.is_system_admin || role === 'system_admin';
  $('us-events').innerHTML = S.events.map((e) =>
    `<label class="check"><input type="checkbox" value="${e.id}">
      <span>${esc(e.name)}</span></label>`).join('')
    || '<p class="muted">No events yet.</p>';
}

$('us-role').addEventListener('change', renderUserEvents);

$('user-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('user-error').hidden = true;
  try {
    await post('/api/setup/users', {
      username: $('us-name').value,
      display_name: $('us-display').value,
      password: $('us-pass').value,
      role: $('us-role').value,
      organization_id: S.user.is_system_admin && $('us-role').value !== 'system_admin'
        ? Number($('us-org').value) : undefined,
      event_ids: [...$('us-events').querySelectorAll('input:checked')]
        .map((c) => Number(c.value)),
    });
    $('user-form').reset();
    banner('Administrator created.');
    loadUsers();
  } catch (err) {
    $('user-error').textContent = err.message;
    $('user-error').hidden = false;
  }
});

/* ---------- start -------------------------------------------------------- */

async function start() {
  $('gate').hidden = true;
  $('app').hidden = false;
  $('whoami').hidden = false;
  $('whoami').textContent = `${S.user.display_name || S.user.username} · ${S.user.role_label}`;
  $('logout').hidden = false;
  document.querySelector('[data-tab="users"]').hidden = !S.user.may_manage_users;
  document.querySelector('[data-tab="orgs"]').hidden = !S.user.is_system_admin;
  if (S.user.is_system_admin) await loadOrgs();
  await loadEvents();
  // One event is the normal case for a club, so select it rather than making
  // them pick from a list of one.
  if (S.events.length === 1) selectEvent(S.events[0].id);
}

(async () => {
  try {
    const data = await api('/api/setup/session');
    if (data.user) { S.user = data.user; await start(); }
    else showGate(data.first_run);
  } catch (err) {
    showGate(window.__FIRST_RUN__ === true);
  }
})();
