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
  // Already signed in but still looking at the sign-in form: recover rather
  // than leaving the header and the form contradicting each other.
  if (response.status === 409 && S.user === null) {
    const check = await fetch('/api/setup/session').then((r) => r.json())
      .catch(() => ({}));
    if (check.user) { S.user = check.user; await start(); }
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Error ${response.status}`);
  return data;
}

const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});

/* ---------- sign in ------------------------------------------------------ */

function showGate(firstRun, notice) {
  $('gate').hidden = false;
  $('app').hidden = true;
  $('whoami').hidden = true;
  $('logout').hidden = true;
  const banner = $('gate-notice');
  banner.textContent = notice || '';
  banner.hidden = !notice;
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
  const submit = $('gate-submit');

  // Password hashing is deliberately slow - memory-hard, a few hundred
  // milliseconds. Without a busy state the form looks dead, and the natural
  // response is to press the button again, which races: both requests see no
  // users, both try to create one, and the loser reports a confusing error.
  if (submit.disabled) return;
  submit.disabled = true;
  const label = submit.textContent;
  submit.textContent = firstRun ? 'Creating account…' : 'Signing in…';
  $('gate-error').hidden = true;
  $('gate-notice').hidden = true;

  try {
    const body = {
      username: $('gate-username').value,
      password: $('gate-password').value,
    };
    if (firstRun) body.display_name = $('gate-display').value;

    if (firstRun) {
      await post('/api/setup/first-user', body);
      // Sign in rather than being let straight through: typing the password
      // once now proves it works, while it is still fresh.
      const username = $('gate-username').value;
      showGate(false, 'Account created. Sign in with it to continue.');
      $('gate-username').value = username;
      $('gate-password').value = '';
      $('gate-password').focus();
    } else {
      const data = await post('/api/setup/login', body);
      S.user = data.user;
      await start();
    }
  } catch (err) {
    $('gate-error').textContent = err.message;
    $('gate-error').hidden = false;
    // A 409 means the account exists after all - most likely this person's own
    // double submit - so put them on the sign-in form rather than leaving them
    // staring at an error on a form that can never succeed again.
    if (/already complete/i.test(err.message)) {
      const username = $('gate-username').value;
      showGate(false, 'Your account was created. Sign in with it to continue.');
      $('gate-username').value = username;
      $('gate-error').hidden = true;
    }
  } finally {
    submit.disabled = false;
    if (submit.textContent.endsWith('…')) submit.textContent = label;
  }
});

$('logout').addEventListener('click', async () => {
  await post('/api/setup/logout');
  location.reload();
});

/* ---------- tabs --------------------------------------------------------- */

function activateTab(name) {
  document.querySelectorAll('.tab').forEach(
    (t) => t.classList.toggle('is-on', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => {
    p.hidden = p.dataset.panel !== name;
  });
  refreshTab(name);
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => activateTab(tab.dataset.tab));
});
document.querySelectorAll('[data-goto]').forEach((b) => {
  b.addEventListener('click', () => activateTab(b.dataset.goto));
});

function needEvent() {
  if (S.eventId) return true;
  banner('Pick an event first.', true);
  return false;
}

/* An event-scoped tab is meaningless without an event, and its form would post
   to /events/null/... and fail with something unhelpful. Hide the panel's
   contents and say what to do instead.

   Only what this gate hid is put back: elements already hidden for their own
   reasons - a Cancel button, an error line, the review section - must stay
   that way, or picking an event would reveal half-built UI. */
function gateOnEvent(name) {
  const panel = document.querySelector(`.panel[data-panel="${name}"]`);
  if (!panel || !panel.hasAttribute('data-needs-event')) return true;
  const ok = !!S.eventId;
  panel.querySelectorAll(':scope > *').forEach((el) => {
    if (el.classList.contains('needs-event')) {
      el.hidden = ok;
    } else if (!ok && !el.hidden) {
      el.dataset.gated = '1';
      el.hidden = true;
    } else if (ok && el.dataset.gated) {
      delete el.dataset.gated;
      el.hidden = false;
    }
  });
  return ok;
}

async function refreshTab(name) {
  try {
    if (name === 'orgs') return loadOrgs();
    if (name === 'events') return loadEvents();
    if (name === 'users') return loadUsers();
    if (!gateOnEvent(name)) return;
    if (name === 'course') return loadStaged();
    if (name === 'stations') return loadCourses();
    if (name === 'layers' || name === 'roles') return loadLayers();
    if (name === 'roster') return loadRoster();
    if (name === 'links') return loadLinks();
  } catch (err) { banner(err.message, true); }
}

/* ---------- icon buttons -------------------------------------------------- */

/* Row actions repeat on every line, and spelled out they crowd the table more
   than the data does.

   Inline SVG rather than glyphs or an icon font. Glyphs were tried first and
   are not dependable: U+270E with the U+FE0E text-presentation selector still
   came out as a full-colour emoji pencil in Chrome on Windows, which reads as
   decoration and fights the status colours that mean something in this app.
   An icon font is one more file to ship and to fail to load, and the frontend
   deliberately has no build step. SVG paths inherit currentColor, so a danger
   button's icon turns red with its text, and stay crisp at any zoom. */
const ICONS = {
  edit: ['<path d="M11.6 2.6a1.6 1.6 0 0 1 2.3 2.3l-7.4 7.4-3 .7.7-3z"/>', 'Edit'],
  save: ['<path d="M3 8.4l3.6 3.6L13.4 5"/>', 'Save'],
  remove: ['<path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"/>', 'Remove'],
  copy: ['<rect x="6" y="6" width="7.4" height="7.4" rx="1.4"/>'
       + '<path d="M10.6 3.6H4.2a1.6 1.6 0 0 0-1.6 1.6v6.4"/>', 'Copy link'],
  password: ['<circle cx="6.2" cy="9.8" r="2.6"/>'
           + '<path d="M8.1 8L13.4 2.7M11.4 4.7l1.5 1.5"/>', 'Set a password'],
  // Two columns of dots: the conventional "drag me" grip. Recognised without
  // explanation, which a label on every row could not be.
  grip: ['<path d="M6.2 4.2h.01M9.8 4.2h.01M6.2 8h.01M9.8 8h.01'
       + 'M6.2 11.8h.01M9.8 11.8h.01"/>', 'Reorder'],
};

/* Icon-only buttons are invisible to a screen reader and to anyone who does not
   recognise the shape, so every one carries both an aria-label and a title -
   the first for assistive technology, the second as a hover tooltip. The label
   names the row as well as the verb ("Delete Aid 3"), because in a table of
   near-identical rows "Delete" alone does not say which one. */
function iconBtn(kind, attrs, label) {
  const [path, fallback] = ICONS[kind];
  const text = label || fallback;
  const danger = kind === 'remove' ? ' danger' : '';
  const pairs = Object.entries(attrs)
    .map(([k, v]) => `${k}="${esc(String(v))}"`).join(' ');
  return `<button type="button" class="icon-btn${danger}" ${pairs} `
    + `title="${esc(text)}" aria-label="${esc(text)}">`
    + '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" '
    + 'fill="none" stroke="currentColor" stroke-width="1.5" '
    + `stroke-linecap="round" stroke-linejoin="round">${path}</svg></button>`;
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
        ? iconBtn('edit', {'data-edito': o.id}, `Edit ${o.name}`)
          + iconBtn('remove', {'data-delo': o.id}, `Delete ${o.name}`) : ''}</td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">No organizations yet.</p>';

  $('org-list').querySelectorAll('[data-edito]').forEach((b) =>
    b.addEventListener('click', () => {
      editOrg(S.organizations.find((o) => o.id === Number(b.dataset.edito)));
    }));
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

/* The short name is left alone when editing. Nothing outside this screen shows
   it, so renaming it fixes nothing a club would notice. */
function editOrg(org) {
  if (!org) return;
  S.editingOrg = org.id;
  $('org-form-title').textContent = `Edit ${org.name}`;
  $('og-slug').value = org.slug;
  $('og-slug').disabled = true;
  $('og-name').value = org.name;
  $('og-contact').value = org.contact || '';
  $('org-submit').textContent = 'Save changes';
  $('org-cancel').hidden = false;
  $('og-name').focus();
}

function resetOrgForm() {
  S.editingOrg = null;
  $('org-form').reset();
  $('og-slug').disabled = false;
  $('org-form-title').textContent = 'Add an organization';
  $('org-submit').textContent = 'Create organization';
  $('org-cancel').hidden = true;
  $('org-error').hidden = true;
}

$('org-cancel').addEventListener('click', () => resetOrgForm());

$('org-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('org-error').hidden = true;
  try {
    if (S.editingOrg) {
      const saved = await post(`/api/setup/organizations/${S.editingOrg}`, {
        name: $('og-name').value, contact: $('og-contact').value,
      });
      resetOrgForm();
      banner(`Saved ${saved.name}.`);
      loadOrgs();
      return;
    }
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
        <td>${esc(e.event_date || '')}<br>
          <span class="muted">${esc(e.timezone || '')}</span></td>
        <td>${e.counts.courses}</td>
        <td>${e.counts.pois}</td>
        <td>${e.counts.roster}</td>
        <td class="actions">
          <button type="button" data-pick="${e.id}" aria-pressed="${e.id === S.eventId}"
            >${e.id === S.eventId ? 'Selected' : 'Select'}</button>
          ${iconBtn('edit', {'data-edite': e.id}, `Edit ${e.name}`)}
          ${S.user.is_system_admin
            ? iconBtn('remove', {'data-del': e.id}, `Delete ${e.name}`) : ''}
        </td></tr>`).join('') + '</tbody></table>';

  host.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
    selectEvent(Number(b.dataset.pick));
  }));
  showEventContext();

  host.querySelectorAll('[data-edite]').forEach((b) => b.addEventListener('click', () => {
    editEvent(S.events.find((e) => e.id === Number(b.dataset.edite)));
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

/* Re-read from S.events rather than remembering the name, so a rename shows up
   here too. Otherwise the header keeps announcing the old name for the rest of
   the session, which is the sort of quiet contradiction that makes someone
   distrust the whole screen. */
function showEventContext() {
  const event = S.events.find((e) => e.id === S.eventId);
  $('event-context').textContent = event ? `Working on: ${event.name}` : '';
  $('event-context').hidden = !event;
}

function selectEvent(id) {
  S.eventId = id;
  showEventContext();
  document.querySelectorAll('.panel[data-needs-event]').forEach(
    (p) => gateOnEvent(p.dataset.panel));
  loadEvents();
}

/* Typing an IANA zone name from memory is a way to get it subtly wrong -
   "US/Central" and "America/Chicago" both look right, and the mistake shows up
   as times an hour out on race morning. North America first, because that is
   who runs these events; the browser's own zone is added if it is not already
   listed, so a club anywhere still finds theirs. */
const TIME_ZONES = [
  ['America/New_York', 'Eastern - New York'],
  ['America/Chicago', 'Central - Chicago'],
  ['America/Denver', 'Mountain - Denver'],
  ['America/Phoenix', 'Mountain, no DST - Phoenix'],
  ['America/Los_Angeles', 'Pacific - Los Angeles'],
  ['America/Anchorage', 'Alaska - Anchorage'],
  ['Pacific/Honolulu', 'Hawaii - Honolulu'],
  ['America/Puerto_Rico', 'Atlantic - Puerto Rico'],
  ['America/St_Johns', 'Newfoundland - St. Johns'],
  ['America/Halifax', 'Atlantic Canada - Halifax'],
  ['America/Toronto', 'Eastern Canada - Toronto'],
  ['America/Winnipeg', 'Central Canada - Winnipeg'],
  ['America/Edmonton', 'Mountain Canada - Edmonton'],
  ['America/Vancouver', 'Pacific Canada - Vancouver'],
  ['UTC', 'UTC'],
];

function fillTimeZones() {
  let here = '';
  try {
    here = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (err) {
    here = '';
  }

  const zones = TIME_ZONES.slice();
  if (here && !zones.some(([id]) => id === here)) zones.unshift([here, here]);

  $('ev-tz').innerHTML = zones
    .map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('');
  // The event is nearly always in the zone of the person setting it up.
  $('ev-tz').value = zones.some(([id]) => id === here) ? here : 'America/Chicago';
}

/* The short name is NOT editable after creation. It is the /e/<slug>/<token>
   in every link already handed out, so changing it would 404 every volunteer
   holding one - silently, and on the morning they need it. Renaming the event
   changes the name shown; the link keeps the old short name, which nobody but
   the coordinator ever reads. */
function editEvent(event) {
  if (!event) return;
  S.editingEvent = event.id;
  $('event-form').hidden = false;
  $('event-form-title').textContent = `Edit ${event.name}`;
  $('ev-slug').value = event.slug;
  $('ev-slug').disabled = true;
  $('ev-slug-note').hidden = false;
  $('ev-name').value = event.name;
  $('ev-date').value = event.event_date || '';
  if (event.timezone) $('ev-tz').value = event.timezone;
  $('ev-org-field').hidden = true;
  $('event-submit').textContent = 'Save changes';
  $('event-cancel').hidden = false;
  $('ev-name').focus();
}

function resetEventForm() {
  S.editingEvent = null;
  $('event-form').reset();
  $('ev-slug').disabled = false;
  $('ev-slug-note').hidden = true;
  $('event-form-title').textContent = 'New event';
  $('event-submit').textContent = 'Create event';
  $('event-cancel').hidden = true;
  $('event-error').hidden = true;
  $('ev-org-field').hidden = !S.user.is_system_admin;
  fillTimeZones();
}

$('event-cancel').addEventListener('click', () => resetEventForm());

$('event-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('event-error').hidden = true;
  try {
    if (S.editingEvent) {
      const saved = await post(`/api/setup/events/${S.editingEvent}`, {
        name: $('ev-name').value,
        event_date: $('ev-date').value,
        timezone: $('ev-tz').value,
      });
      resetEventForm();
      banner(`Saved ${saved.name}.`);
      await loadEvents();
      return;
    }
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

/* The upload has always looked like a drop zone - a dashed border says exactly
   that - while accepting clicks only. Dropping a file did nothing at all: no
   import, no error, nothing to say whether the file was wrong, the app was
   broken, or you missed. A control that promises something it does not do is
   worse than a plain button.

   Files arrive from an organizer by email, so dragging one out of a mail client
   is the natural gesture. */
const uploadZone = $('upload-zone');

['dragenter', 'dragover'].forEach((name) =>
  uploadZone.addEventListener(name, (ev) => {
    ev.preventDefault();
    uploadZone.classList.add('is-over');
  }));

['dragleave', 'drop'].forEach((name) =>
  uploadZone.addEventListener(name, () =>
    uploadZone.classList.remove('is-over')));

uploadZone.addEventListener('drop', (ev) => {
  ev.preventDefault();
  importFiles([...(ev.dataTransfer ? ev.dataTransfer.files : [])]);
});

/* Import several files one after another.

   In sequence, not in parallel: import is additive and each file stages into
   the same event, so concurrent uploads race on the review list and the counts
   come back wrong. An organizer's courses routinely arrive as one file per
   race, so dropping four at once is the normal case rather than the clever one.
*/
async function importFiles(files) {
  if (!files.length || !needEvent()) return;

  const usable = files.filter((f) => /\.(kml|kmz)$/i.test(f.name));
  const rejected = files.filter((f) => !/\.(kml|kmz)$/i.test(f.name));
  if (rejected.length) {
    // Say so rather than ignoring them. A dropped .gpx failing in silence is
    // indistinguishable from the app being broken - see issue #1 for GPX.
    banner(`Not a KML or KMZ: ${rejected.map((f) => f.name).join(", ")}`, true);
  }
  if (!usable.length) return;

  for (const [index, file] of usable.entries()) {
    $('import-status').hidden = false;
    $('import-status').textContent = usable.length > 1
      ? `Reading ${file.name} (${index + 1} of ${usable.length})…`
      : `Reading ${file.name}…`;
    try {
      await uploadCourseFile(file);
    } catch (err) {
      banner(`${file.name}: ${err.message}`, true);
      break;      // stop rather than plough on; the rest may depend on this one
    }
  }
  $('import-status').hidden = true;
  $('course-file').value = '';
  loadStaged();
}

$('course-file').addEventListener('change', (ev) => {
  importFiles([...ev.target.files]);
});

/* Upload one file. Throws so the caller can stop a run of several rather than
   ploughing on after a failure that the rest may depend on. */
async function uploadCourseFile(file) {
  const form = new FormData();
  form.append('file', file);
  const response = await fetch(`/api/setup/events/${S.eventId}/import`,
    {method: 'POST', body: form});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Import failed');

  const kinds = Object.entries(data.by_type)
    .map(([k, n]) => `${n} ${k}`).join(', ');
  banner(`${data.filename}: ${data.total} features (${kinds}).`);
  // Warnings are the interesting part - a layer created from the file's own
  // attributes, or segments that could not be joined - so they get their own
  // line rather than being reduced to a count.
  (data.warnings || []).forEach((w) => banner(w, true));
  return data;
}

/* The review screen offers whatever layers this event has, so importing a
   medic or mile-marker layer needs no code change. "Course" stays first and
   fixed: a route is not a place, and it is what most features become. */
async function fillAssignTypes() {
  if (!S.poiCategories) {
    const data = await api(`/api/setup/events/${S.eventId}/categories`);
    S.poiCategories = data.poi_categories;
  }
  $('assign-type').innerHTML = '<option value="course">Course</option>'
    + S.poiCategories.map((c) =>
        `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
}

async function loadStaged() {
  await fillAssignTypes();
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

  renderBulkBar();
}

/* One file can stage ninety features - a real organizer's points file carried
   78 - and ticking them one at a time is not review, it is data entry that
   gets abandoned half way. */
function suggestedPlaces() {
  return S.staged.filter(
    (f) => f.geom_type === 'point'
        && f.suggestion && f.suggestion.startsWith('poi:'));
}

function renderBulkBar() {
  const suggested = suggestedPlaces();
  const button = $('accept-suggested');
  button.hidden = suggested.length === 0;
  if (suggested.length) {
    // Name the layers rather than just a count: accepting 78 assignments
    // blind is not a decision, and the exporter can be wrong.
    const layers = [...new Set(suggested.map((f) => f.suggestion.slice(4)))];
    const names = layers
      .map((key) => (S.poiCategories.find((c) => c.key === key) || {}).name || key);
    button.textContent =
      `Accept ${suggested.length} suggested (${names.join(', ')})`;
  }
  updatePickCount();
}

function updatePickCount() {
  $('pick-count').textContent = S.picked.size
    ? `${S.picked.size} selected` : '';
}

function setAllPicked(on) {
  S.picked.clear();
  if (on) S.staged.forEach((f) => S.picked.add(f.id));
  S.staged.forEach((f) => {
    const row = $('review-list').querySelector(`[data-id="${f.id}"]`);
    if (row) {
      row.classList.toggle('is-picked', on);
      row.querySelector('input').checked = on;
    }
    const layer = S.layers.get(f.id);
    if (layer && layer.setStyle) {
      layer.setStyle(on ? {color: '#FF6A13', weight: 6}
                        : {color: '#0B2545', weight: 4});
    }
  });
  $('review-actions').hidden = S.picked.size === 0;
  updatePickCount();
}

$('pick-all').addEventListener('click', () => setAllPicked(true));
$('pick-none').addEventListener('click', () => setAllPicked(false));

/* Assign every place the file already classified, grouped by layer.
   The exporter stated what each point is, so this is accepting its word rather
   than guessing - and it is still a person pressing the button, which is the
   rule that keeps a parking lot from filing itself as an aid station. */
$('accept-suggested').addEventListener('click', async () => {
  const groups = new Map();
  suggestedPlaces().forEach((f) => {
    const key = f.suggestion.slice(4);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  });

  let assigned = 0;
  try {
    for (const [key, features] of groups) {
      // One call per layer, in sequence. In parallel they would race on the
      // same staged rows and the counts would come back wrong.
      await post(`/api/setup/events/${S.eventId}/assign`, {
        kind: 'poi',
        ids: features.map((f) => f.id),
        poi_type: key,
        // No name: each place keeps the label the import gave it, which for
        // an attribute-carrying file is already "MM 12" rather than the race.
        name: '',
      });
      assigned += features.length;
    }
    banner(`Assigned ${assigned} place(s) into ${groups.size} layer(s).`);
    loadStaged();
  } catch (err) { banner(err.message, true); }
});

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
  // The layer list drives the per-row dropdown and the bulk target.
  if (!S.poiCategories) {
    S.poiCategories = (await api(
      `/api/setup/events/${S.eventId}/categories`)).poi_categories;
  }
  const data = await api(`/api/setup/events/${S.eventId}/courses`);
  S.pois = data.pois;

  // The Places table needs these to offer a checkbox per race.
  S.courses = data.courses;

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
      <td class="actions">${iconBtn('save', {'data-savec': c.id}, `Save ${c.name}`)
        + iconBtn('remove', {'data-delc': c.id}, `Delete ${c.name}`)}</td>
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

  /* Open the what3words map on a square, so the words can be read off and
     pasted into the field beside this link.

     We deliberately do not integrate the what3words API - it is paid, and a
     club should not need an account (see docs/PLAN.md). A link costs nothing
     and turns "go and find this point on a map somehow" into one click.

     This lat/lng form of the URL is NOT documented by what3words; the
     documented links are word-based, and the app URI scheme (w3w://show)
     has no coordinate form at all. Verified working 2026-09-03: the site
     rewrites the URL to the square it resolved, e.g.
       ?lat=44.1636&lng=-93.9994  ->  /clip.apples.leap?zoom=19&lat=...
     If it ever stops resolving, the fallback is the coordinates in the
     column beside it, which are ours and cannot break. */
  const w3wLookup = (lat, lon) =>
    `https://what3words.com/?maptype=roadmap&zoom=19`
    + `&lat=${lat.toFixed(6)}&lng=${lon.toFixed(6)}`;

  $('poi-table').innerHTML = data.pois.length ? `
    <table class="grid"><thead><tr><th></th><th><input type="checkbox" id="poi-all"
        aria-label="Select every place"></th>
      <th>Mile</th><th>Name</th><th>Layer</th>
      <th>Races</th><th>Pin</th><th>Coordinates</th><th>What3Words</th>
      <th></th></tr></thead><tbody>` +
    data.pois.map((p) => `<tr data-row="${p.id}">
      <td class="grip-cell">${iconBtn('grip', {'data-grip': p.id},
        `Reorder ${p.name} - drag, or use the arrow keys`)}</td>
      <td><input type="checkbox" data-ppick="${p.id}"
            aria-label="Select ${esc(p.name)}"></td>
      <td class="mile">${p.distance_along_m != null
        ? `${miles(p.distance_along_m)}<br><span class="muted"
             >${esc(p.course_name || '')}</span>`
        : '—'}</td>
      <td><input value="${esc(p.name)}" data-pname="${p.id}" style="width:150px"></td>
      <td><span class="layer-glyph" style="color:${esc(p.layer_color || '#35507a')}"
            >${glyphSvg(p.layer_icon || 'pin', 16)}</span>
        <select data-player="${p.id}" style="width:150px">${
          S.poiCategories.map((c) => `<option value="${esc(c.key)}"${
            c.key === p.poi_type ? ' selected' : ''}>${esc(c.name)}</option>`).join('')
        }</select></td>
      <td class="races">${S.courses.length ? S.courses.map((c) => `
        <label class="race-pick"><input type="checkbox" data-prace="${p.id}"
            value="${c.id}"${(p.course_ids || []).includes(c.id) ? ' checked' : ''}
            aria-label="${esc(p.name)} serves ${esc(c.name)}"
          ><span>${esc(c.name)}</span></label>`).join('')
        : '<span class="muted">no courses</span>'}
        <input type="hidden" data-pcourses="${p.id}"
          value="${(p.course_ids || []).join(',')}"></td>
      <td>${p.show_labels
        ? `<input value="${esc(p.label || '')}" data-plabel="${p.id}"
             class="pin-label" maxlength="2"
             placeholder="${esc(p.label_auto || '')}"
             title="Drawn on the pin. Blank uses ${esc(p.label_auto || 'nothing')}."
             aria-label="Pin label for ${esc(p.name)}">`
        : '<span class="muted" title="Turn labels on for this layer">off</span>'}</td>
      <td class="coords">${p.lat != null
        ? `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}` : '\u2014'}</td>
      <td class="w3w-cell"><input value="${esc(p.what3words || '')}"
            data-w3w="${p.id}" placeholder="filled.count.soap"
            style="width:150px">${p.lat != null
        ? `<a class="w3w-link" target="_blank" rel="noopener"
              href="${w3wLookup(p.lat, p.lon)}"
              title="Open what3words at ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}"
              >///</a>` : ''}</td>
      <td class="actions">${iconBtn('remove', {'data-delp': p.id},
        `Delete ${p.name}`)}</td>
    </tr>`).join('') + '</tbody></table>'
    : '<p class="muted">No aid stations yet.</p>';

  /* Sorting a flat import one row at a time is the kind of chore that gets
     abandoned half-finished, and a half-sorted map lies about what is where. */
  const picked = () => [...$('poi-table').querySelectorAll('[data-ppick]:checked')]
    .filter((c) => c.closest('tr').style.display !== 'none')
    .map((c) => Number(c.dataset.ppick));

  /* A flat import can leave eighty places in one table, and finding the four
     that belong somewhere else means scrolling past seventy-six that do not.
     Filtering is on the name AND the layer, because both are how someone
     thinks about these: "the mile markers" and "Alpha" are the same search. */
  function applyPoiFilter() {
    const needle = ($('poi-filter').value || '').trim().toLowerCase();
    const wantLayer = $('poi-filter-layer').value;
    let shown = 0;
    $('poi-table').querySelectorAll('tbody tr').forEach((tr) => {
      const name = (tr.querySelector('[data-pname]') || {}).value || '';
      const layer = tr.querySelector('[data-player]');
      const layerName = layer ? layer.options[layer.selectedIndex].text : '';
      // The two narrow together, which is what makes splitting one imported
      // layer practical: pick "Mile markers", type "FULL", and you have the
      // 29 that belong to the marathon rather than all 48.
      const matchesLayer = !wantLayer || (layer && layer.value === wantLayer);
      const hit = matchesLayer && (!needle
        || name.toLowerCase().includes(needle)
        || layerName.toLowerCase().includes(needle));
      tr.style.display = hit ? '' : 'none';
      // A hidden row must not stay selected. Moving something you cannot see
      // because it matched a filter you have since changed is the kind of
      // surprise that makes people stop trusting bulk actions.
      if (!hit) {
        const box = tr.querySelector('[data-ppick]');
        if (box) box.checked = false;
      }
      if (hit) shown += 1;
    });
    const total = $('poi-table').querySelectorAll('tbody tr').length;
    $('poi-shown').textContent = (needle || wantLayer)
      ? `${shown} of ${total} shown` : '';
    refreshPoiSelection();
  }

  $('poi-filter').oninput = applyPoiFilter;
  $('poi-filter-layer').onchange = (ev) => {
    S.poiFilterLayer = ev.target.value;
    applyPoiFilter();
  };
  // Rebuilding the options resets the selection, which silently threw away the
  // filter every time the table reloaded. Put it back.
  $('poi-filter-layer').innerHTML = '<option value="">All layers</option>'
    + S.poiCategories.map((c) =>
        `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
  if (S.poiFilterLayer) $('poi-filter-layer').value = S.poiFilterLayer;

  function refreshPoiSelection() {
    const n = picked().length;
    $('poi-bulk').hidden = n === 0;
    $('poi-selected').textContent = n === 1 ? '1 place selected'
      : `${n} places selected`;
  }

  $('poi-move-to').innerHTML = S.poiCategories
    .map((c) => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
  $('poi-table').querySelectorAll('[data-ppick]').forEach((c) =>
    c.addEventListener('change', refreshPoiSelection));
  if ($('poi-all')) {
    $('poi-all').addEventListener('change', (ev) => {
      // Visible rows only. With a filter applied, "select all" meaning
      // "including the seventy you filtered out" would be a trap.
      $('poi-table').querySelectorAll('[data-ppick]').forEach((c) => {
        if (c.closest('tr').style.display !== 'none') {
          c.checked = ev.target.checked;
        }
      });
      refreshPoiSelection();
    });
  }
  /* Races are several checkboxes but one stored value, so they write into a
     hidden field. bindSaveAll then sees an ordinary changed field and needs to
     know nothing about lists.

     Left blank, a place is "not stated" and the lead runner panel falls back
     to snapping it onto the nearest course line. That fallback is the thing
     this replaces - it picks exactly ONE race for a stop that may serve three,
     which made a race's progression skip stops - but it has to stay, because
     it is what every event created before this has. */
  $('poi-table').querySelectorAll('[data-prace]').forEach((box) => {
    box.addEventListener('change', () => {
      const row = box.closest('tr');
      const chosen = [...row.querySelectorAll('[data-prace]:checked')]
        .map((c) => c.value);
      const hidden = row.querySelector('[data-pcourses]');
      hidden.value = chosen.join(',');
      // The hidden input is not what changed, so nothing has fired the input
      // event the dirty tracker listens for.
      hidden.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });

  /* Drag a row to set the running order.

     Geometry cannot do this once an event has more than one route. Each place
     is snapped to whichever course line is nearest - a coin flip where routes
     share pavement - so the list interleaves miles measured on three different
     races. Which stop follows which is a fact the club holds; this is where
     they say it.

     The order is taken from the DOM after a move, so it stays correct with a
     filter applied: hidden rows keep their positions and simply travel with
     whatever is around them. */
  const poiTableEl = $('poi-table');

  async function persistOrder() {
    const ids = [...poiTableEl.querySelectorAll('tbody tr[data-row]')]
      .map((tr) => Number(tr.dataset.row));
    if (!ids.length) return;
    try {
      await post(`/api/setup/events/${S.eventId}/pois/reorder`, { poi_ids: ids });
      $('poi-order-note').textContent = 'Order saved.';
    } catch (err) {
      // Say so. A silently unsaved order looks identical to a saved one until
      // the page is reloaded, which is the worst moment to find out.
      $('poi-order-note').textContent = `Order NOT saved: ${err.message}`;
      banner(err.message, true);
    }
  }

  let dragRow = null;

  poiTableEl.querySelectorAll('[data-grip]').forEach((grip) => {
    const row = grip.closest('tr');
    // The row is only draggable while the grip is held. A permanently
    // draggable row swallows text selection in the inputs inside it.
    grip.addEventListener('mousedown', () => { row.draggable = true; });
    grip.addEventListener('touchstart', () => { row.draggable = true; },
      { passive: true });

    /* Arrow keys do the same job. Drag and drop is unusable with a keyboard,
       and unreliable on a touch screen - and this is a table someone may well
       be sorting on a tablet the morning of the race. */
    grip.addEventListener('keydown', (ev) => {
      const step = ev.key === 'ArrowUp' ? -1 : ev.key === 'ArrowDown' ? 1 : 0;
      if (!step) return;
      ev.preventDefault();
      const sibling = step < 0
        ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling) return;
      if (step < 0) row.parentNode.insertBefore(row, sibling);
      else row.parentNode.insertBefore(sibling, row);
      grip.focus();
      persistOrder();
    });
  });

  poiTableEl.addEventListener('dragstart', (ev) => {
    dragRow = ev.target.closest('tr[data-row]');
    if (dragRow) dragRow.classList.add('is-dragging');
  });

  poiTableEl.addEventListener('dragover', (ev) => {
    if (!dragRow) return;
    ev.preventDefault();
    const over = ev.target.closest('tr[data-row]');
    if (!over || over === dragRow) return;
    const box = over.getBoundingClientRect();
    const after = (ev.clientY - box.top) > box.height / 2;
    over.parentNode.insertBefore(dragRow, after ? over.nextSibling : over);
  });

  poiTableEl.addEventListener('dragend', () => {
    if (!dragRow) return;
    dragRow.classList.remove('is-dragging');
    dragRow.draggable = false;
    dragRow = null;
    persistOrder();
  });

  /* Save the table as a unit. A save button per row used to reload the whole
     list, discarding every other edit in progress. */
  bindSaveAll({
    table: 'poi-table',
    button: 'poi-save-all',
    status: 'poi-dirty',
    fields: [
      { attr: 'pname', name: 'name' },
      { attr: 'w3w', name: 'what3words' },
      { attr: 'player', name: 'poi_type' },
      { attr: 'plabel', name: 'label' },
      { attr: 'pcourses', name: 'course_ids' },
    ],
    save: (id, payload) =>
      post(`/api/setup/events/${S.eventId}/pois/${id}`, payload),
    noun: 'place(s)',
    reload: loadCourses,
  });
  applyPoiFilter();

  $('poi-table').querySelectorAll('[data-delp]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('Delete this aid station?')) return;
      await post(`/api/setup/events/${S.eventId}/pois/${b.dataset.delp}/delete`);
      loadCourses();
    }));
}

/* ---------- layers ------------------------------------------------------- */

/* The kinds of place this event has, and the club's own names for the station
   roles. Both exist because the taxonomy is not the code's to decide: a KML
   arrives with whatever the organizer drew, and one club's "Rover" is
   another's "Floater". */

let iconPick = 'pin';

function renderIconPicker(host, selected, onPick) {
  host.innerHTML = Object.keys(POI_GLYPHS).map((key) =>
    `<button type="button" class="icon-opt${key === selected ? ' is-on' : ''}"
       data-icon="${key}" title="${esc(glyphLabel(key))}"
       aria-label="${esc(glyphLabel(key))}"
       aria-pressed="${key === selected}">${glyphSvg(key, 18)}</button>`).join('');
  host.querySelectorAll('[data-icon]').forEach((b) =>
    b.addEventListener('click', () => {
      onPick(b.dataset.icon);
      renderIconPicker(host, b.dataset.icon, onPick);
    }));
}

/* One save button for a whole table.

   Every editable table in setup used to save one row at a time, and each save
   had to reload the table to show the result - which threw away every other
   edit in progress. It cost a real user twelve renamed water stops, and the
   Layers tab was worse still: saving a role reloaded the layers table above
   it, so edits vanished from a table nobody had touched.

   So a table saves as a unit. This tracks what actually changed against what
   was rendered, sends only those fields, and reloads once at the end. `fields`
   maps a data-attribute to the payload field it fills; the first one is also
   the row key, and a checkbox sends a boolean.

   Do not go back to a save button per row. Two save buttons with different
   scopes is how the original bug happened. */
function bindSaveAll({ table, button, status, fields, save, noun, reload }) {
  const root = $(table);
  const btn = $(button);
  const read = (el) => (el.type === 'checkbox' ? String(el.checked) : el.value);

  root.querySelectorAll(fields.map((f) => `[data-${f.attr}]`).join(', '))
    .forEach((el) => { el.dataset.original = read(el); });

  function dirty() {
    const changed = new Map();
    root.querySelectorAll('tbody tr').forEach((tr) => {
      const first = tr.querySelector(`[data-${fields[0].attr}]`);
      if (!first) return;
      const payload = {};
      fields.forEach(({ attr, name }) => {
        const el = tr.querySelector(`[data-${attr}]`);
        // A field can be absent from a row for its own reasons - a pin label
        // on a layer that is not labelled - and that is not a change.
        if (!el || read(el) === el.dataset.original) return;
        payload[name] = el.type === 'checkbox' ? el.checked : el.value;
      });
      if (Object.keys(payload).length) {
        changed.set(first.dataset[fields[0].attr], payload);
      }
    });
    return changed;
  }

  function refresh() {
    const count = dirty().size;
    btn.hidden = count === 0;
    btn.textContent = count === 1 ? 'Save 1 change' : `Save ${count} changes`;
    if (status) $(status).textContent = count ? 'unsaved' : '';
  }

  root.addEventListener('input', refresh);
  root.addEventListener('change', refresh);

  btn.onclick = async () => {
    const changed = dirty();
    if (!changed.size) return;
    btn.disabled = true;
    btn.textContent = `Saving ${changed.size}\u2026`;
    let saved = 0;
    try {
      for (const [key, payload] of changed) {
        await save(key, payload);
        saved += 1;
      }
      banner(`Saved ${saved} ${noun}.`);
    } catch (err) {
      // How far it got, so you know what still needs retyping.
      banner(`Saved ${saved} of ${changed.size}, then: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      reload();
    }
  };

  refresh();
}

/* Layers and roles share one endpoint but are two independent tables on one
   tab, so each re-renders only itself after a save. Re-rendering both would
   discard unsaved edits in the other one - the same bug, one table over. */
async function loadLayers(only) {
  const data = await api(`/api/setup/events/${S.eventId}/categories`);
  S.poiCategories = data.poi_categories;
  S.roles = data.roster_roles;
  if (only !== 'roles') renderLayerTable();
  if (only !== 'layers') renderRoleTable();
}

function renderLayerTable() {
  renderIconPicker($('lay-icon-picker'), iconPick, (k) => { iconPick = k; });

  $('layer-table').innerHTML = `
    <table class="grid"><thead><tr><th></th><th>Layer</th><th>Places</th>
      <th>We staff these</th><th>On by default</th><th>Labels on pins</th>
      <th></th></tr></thead><tbody>`
    + S.poiCategories.map((c) => `<tr>
        <td><span class="layer-glyph" style="color:${esc(c.color || '#35507a')}"
            >${glyphSvg(c.icon, 20)}</span></td>
        <td><input value="${esc(c.name)}" data-lname="${esc(c.key)}" style="width:150px">
          <br><span class="muted">${esc(c.key)}</span></td>
        <td>${c.place_count}</td>
        <td><input type="checkbox" data-lstaffed="${esc(c.key)}"
              ${c.staffed ? 'checked' : ''} aria-label="We staff these"></td>
        <td><input type="checkbox" data-lvisible="${esc(c.key)}"
              ${c.visible ? 'checked' : ''} aria-label="On by default"></td>
        <td><input type="checkbox" data-llabels="${esc(c.key)}"
              ${c.show_labels ? 'checked' : ''}
              aria-label="Show labels on ${esc(c.name)} pins"></td>
        <td class="actions">
          <input type="color" value="${esc(c.color || '#35507a')}"
            data-lcolor="${esc(c.key)}" aria-label="Colour" style="width:44px">
          ${iconBtn('remove', {'data-ldel': c.key}, `Delete ${c.name}`)}
        </td>
      </tr>`).join('') + '</tbody></table>';

  bindSaveAll({
    table: 'layer-table',
    button: 'layer-save-all',
    status: 'layer-dirty',
    fields: [
      { attr: 'lname', name: 'name' },
      { attr: 'lcolor', name: 'color' },
      { attr: 'lstaffed', name: 'staffed' },
      { attr: 'lvisible', name: 'visible' },
      { attr: 'llabels', name: 'show_labels' },
    ],
    save: (key, payload) =>
      post(`/api/setup/events/${S.eventId}/categories/${key}`, payload),
    noun: 'layer(s)',
    reload: () => loadLayers('layers'),
  });

  $('layer-table').querySelectorAll('[data-ldel]').forEach((b) =>
    b.addEventListener('click', async () => {
      const cat = S.poiCategories.find((c) => c.key === b.dataset.ldel);
      if (!confirm(`Delete the "${cat.name}" layer?`)) return;
      try {
        await post(`/api/setup/events/${S.eventId}/categories/${cat.key}/delete`);
        banner(`Deleted ${cat.name}.`);
        loadLayers('layers');
      } catch (err) { banner(err.message, true); }
    }));
}

function renderRoleTable() {
  $('role-table').innerHTML = `
    <table class="grid"><thead><tr><th>Role</th><th>On the roster</th>
      <th></th></tr></thead><tbody>`
    + S.roles.map((r) => `<tr>
        <td><input value="${esc(r.name)}" data-rname="${esc(r.key)}" style="width:180px">
          <br><span class="muted">${esc(r.key)}</span></td>
        <td>${r.in_use || 0}</td>
        <td class="actions">${iconBtn('remove', {'data-rdel': r.key},
          `Delete ${r.name}`)}</td>
      </tr>`).join('') + '</tbody></table>';

  $('role-table').querySelectorAll('[data-rdel]').forEach((b) =>
    b.addEventListener('click', async () => {
      const role = S.roles.find((r) => r.key === b.dataset.rdel);
      if (!confirm(`Delete the "${role.name}" role?`)) return;
      try {
        await post(`/api/setup/events/${S.eventId}/roles/${role.key}/delete`);
        banner(`Deleted ${role.name}.`);
        loadLayers('roles');
      } catch (err) { banner(err.message, true); }
    }));

  bindSaveAll({
    table: 'role-table',
    button: 'role-save-all',
    status: 'role-dirty',
    fields: [{ attr: 'rname', name: 'name' }],
    save: (key, payload) =>
      post(`/api/setup/events/${S.eventId}/roles/${key}`, payload),
    noun: 'role(s)',
    reload: () => loadLayers('roles'),
  });
}

$('role-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  if (!needEvent()) return;
  $('role-error').hidden = true;
  try {
    await post(`/api/setup/events/${S.eventId}/roles`,
      { name: $('role-name').value });
    $('role-name').value = '';
    banner('Role added.');
    loadLayers('roles');
  } catch (err) {
    $('role-error').textContent = err.message;
    $('role-error').hidden = false;
  }
});

$('poi-move').addEventListener('click', async () => {
  const ids = [...$('poi-table').querySelectorAll('[data-ppick]:checked')]
    .map((c) => Number(c.dataset.ppick));
  if (!ids.length) return;
  try {
    const result = await post(`/api/setup/events/${S.eventId}/pois/move`, {
      poi_ids: ids, poi_type: $('poi-move-to').value,
    });
    banner(`Moved ${result.moved} place(s).`);
    loadCourses();
  } catch (err) { banner(err.message, true); }
});

$('layer-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('layer-error').hidden = true;
  try {
    const created = await post(`/api/setup/events/${S.eventId}/categories`, {
      name: $('lay-name').value,
      color: $('lay-color').value,
      icon: iconPick,
      staffed: $('lay-staffed').checked,
    });
    $('layer-form').reset();
    banner(`Added ${created.name}.`);
    loadLayers();
  } catch (err) {
    $('layer-error').textContent = err.message;
    $('layer-error').hidden = false;
  }
});

/* ---------- roster ------------------------------------------------------- */

async function loadRoster() {
  const data = await api(`/api/setup/events/${S.eventId}/roster`);
  S.roster = data.roster;
  S.categories = data.categories;
  S.pois = data.pois;

  // Names come from the server, because a club renames its roles.
  S.roleNames = Object.fromEntries(data.categories.map((c) => [c.key, c.name]));
  $('rs-category').innerHTML = data.categories
    .map((c) => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
  $('rs-poi').innerHTML = '<option value="">Not posted</option>' +
    data.pois.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('');

  $('roster-table').innerHTML = S.roster.length ? `
    <table class="grid"><thead><tr><th>Callsign</th><th>Label</th><th>Operator</th>
      <th>Role</th><th>APRS</th><th>Posted at</th><th></th></tr></thead><tbody>` +
    S.roster.map((r) => `<tr>
      <td><strong>${esc(r.station_key)}</strong>${r.bound_key
        ? `<br><span class="muted">heard as ${esc(r.bound_key)}</span>` : ''}</td>
      <td>${esc(r.display_label)}</td>
      <td>${esc(r.operator_name || '')}</td>
      <td>${esc((S.roleNames || {})[r.category] || r.category)}</td>
      <td>${r.expects_aprs
        ? '<span class="pill">tracked</span>'
        : '<span class="pill is-off">no APRS</span>'}</td>
      <td>${esc(r.poi_name || '')}</td>
      <td class="actions">${iconBtn('edit', {'data-edit': r.station_key},
          `Edit ${r.station_key}`)
        + iconBtn('remove', {'data-delr': r.station_key},
          `Remove ${r.station_key}`)}</td>
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
        ${iconBtn('copy', {'data-copy': url}, `Copy the ${l.role_label} link`)}
        <button type="button" class="danger" data-reissue="${esc(l.role)}"
          >Revoke &amp; reissue</button>
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
        ${iconBtn('password', {'data-pw': u.id}, `Set a password for ${u.username}`)}
        <button type="button" data-toggle="${u.id}"
          data-active="${u.is_active ? '1' : '0'}"
          >${u.is_active ? 'Disable' : 'Enable'}</button>
        ${iconBtn('remove', {'data-delu': u.id}, `Delete ${u.username}`)}
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
  fillTimeZones();
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
