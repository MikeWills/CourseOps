# Security audit

## Summary
Full read of `src/courseops/` (web, access, users, admin, db, hub, ingest, aprsis, parser, kml, gpx, importer, guides, config, resources, incidents, leaders, categories, report, cli, schema.sql), both clients (`static/app.js`, `static/setup.js`, `index.html`, `setup.html`, `icons.js`), `deploy/*` and `.github/workflows/*`. Findings SEC-1, SEC-2, SEC-3, SEC-5 and SEC-7 were reproduced against a throwaway database with the FastAPI test client (script kept at `scratchpad/audit/sec_check.py`; nothing in the repo was touched).
Counts: Critical 0, High 3, Medium 5, Low 10. The two Highs that matter most are cross-event IDORs that break the documented tenancy boundary; the third is an unauthenticated way to stall the live map. SQL parameterisation, HTML escaping in both clients, the guides renderer, token randomness, password hashing and the deploy forced-command are clean.

## Findings

### SEC-1: Staged import features are addressed by id only, so one club can pull another club's course file into its own event and tamper with the other club's pending import
- Severity: High
- File: src/courseops/importer.py:173-176 (`get_feature` has no `event_id`), :268 (`assign_course`), :342 (`assign_poi`), :376-381 (`discard`); called from src/courseops/admin.py:147-181 (`assign_features`) via src/courseops/web.py:847-855
- What: `POST /api/setup/events/{A}/assign` is gated by `require_event_admin(A)`, but the feature ids in the body are looked up with `SELECT * FROM import_feature WHERE id = ?` and never checked against event A. `assign_course` copies the geometry into a course row under event A, `assign_poi` does the same for points, and all three mark the other event's rows `assigned`/`discarded`.
- Evidence: `def get_feature(conn, feature_id): return conn.execute("SELECT * FROM import_feature WHERE id = ?", (feature_id,)).fetchone()`; reproduced: an org-A admin posting `{"kind":"course","ids":[<B's feature id>],"name":"stolen"}` to event A got `200 {"course_id":1,...}` and event A's course list then contained the geometry staged by org B.
- Why it matters: `import_feature.id` is a global autoincrement, so ids are guessable in seconds. Once a second club is hosted (#5), any org admin - or a leaked org-admin session - can lift the other club's organizer KML (the exact data the repo was made private to purge) and can silently `discard` their pending features the week before their race, which looks to them like a failed import. This is exactly the tenancy boundary CLAUDE.md says `may_access_event` is supposed to hold.
- Fix: Give `get_feature` an `event_id` parameter and add `AND event_id = ?` to it and to `discard`; have `assign_course`/`assign_poi`/`discard` take `event_id` and refuse (ValueError) on any id that does not belong. Add a test that assigns a feature from event B into event A and expects 400.
- Effort: S

### SEC-2: Access links are revoked and relabelled by id with no event check, so an admin of one event can kill another event's live links
- Severity: High
- File: src/courseops/access.py:223-239 (`set_label`, `revoke`); src/courseops/web.py:1359-1360, 1376-1377
- What: `POST /api/setup/events/{A}/links` with `{"action":"revoke","token_id":N}` runs `UPDATE access_token SET revoked = 1 WHERE id = ?` for any N. The route checks the caller may administer event A; nothing checks the token belongs to A.
- Evidence: reproduced: org-A admin revoked org-B's first token through event A's endpoint, `200`, and `access_token.revoked` for that row read 1 afterwards.
- Why it matters: Token ids are small sequential integers. A malicious or compromised event/org admin in one club can revoke every NCS, SAG and Liaison link of another club's event on race morning; on the phones this looks like a 404 and there is no error anywhere on the victim's side. Relabelling is the smaller cousin (defacing the label an officer uses to decide which link to revoke). The `reissue` branch is correctly scoped because it iterates `tokens_for_event`.
- Fix: `revoke(conn, event_id, token_id)` / `set_label(conn, event_id, token_id, label)` with `WHERE id = ? AND event_id = ?`, and 404 when `rowcount == 0`. Update `cmd_revoke_link` in cli.py the same way (it takes an event but does not use it for the check either).
- Effort: S

### SEC-3: The login endpoint runs scrypt synchronously on the event loop, unauthenticated and unthrottled, so a few requests per second freeze the live map for everyone
- Severity: High
- File: src/courseops/web.py:685-700 (`async def login` calling `users.authenticate`), :637-683 (`create_first_user`), :713-728; src/courseops/users.py:122-148, 393-406
- What: `login` is an `async def` route, so it runs on uvicorn's single event loop. `users.authenticate` calls `hashlib.scrypt` with N=2^15, r=8 - measured at 0.25-0.36 s per hash on the dev machine (the code comment says "a few tens of milliseconds"), and for an unknown username it hashes twice (see SEC-7), 1.2 s per request end to end. There is no rate limit, no lockout, no per-IP cap, and the endpoint is at the public domain root.
- Evidence: `async def login(request)` -> `users.authenticate(conn, ...)` -> `verify_password` -> `scrypt(..., n=int(n), r=int(r), ...)`; timing from the reproduction: existing user 0.62 s, unknown user 1.20 s, wall clock, in-process.
- Why it matters: While scrypt runs, nothing else is served: WebSocket fan-out (`hub.publish` and `send_json` are on the same loop), `/state` resyncs, incident posts. One unauthenticated client doing `POST /api/setup/login` twice a second with any username takes the map offline for every volunteer for as long as it likes, and the symptom on the phones is the amber "reconnecting" badge - indistinguishable from a bad cell signal. This is the single cheapest way for an outsider to hurt the event.
- Fix: Run the hash off the loop (`await asyncio.to_thread(users.authenticate, ...)` / `run_in_threadpool`), and add a small in-memory limiter on login and first-user (e.g. 5 failures per username or IP per minute, then 429). Fix the comment; consider N=2^14 if a VPS core is slower than the dev box. Keep `create_first_user` and `change_own_password` on the same path.
- Effort: M

### SEC-4: CSRF protection on the cookie-authenticated setup API is SameSite=Lax alone
- Severity: Medium
- File: src/courseops/web.py:544-552 (`_set_session_cookie`), :1678-1687 (`_json_body` accepts any Content-Type), :809-812 (multipart import)
- What: Every mutating setup route is a cookie-authenticated POST with no CSRF token, no Origin/Referer check and no custom-header or Content-Type requirement. `request.json()` parses a body regardless of its `Content-Type`. The only defence is `samesite="lax"`.
- Evidence: `response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure, ...)`; `body = await request.json()` with no header check.
- Why it matters: "Same-site" is the registrable domain, not the origin. Anything else hosted under `*.wx0mik.radio` (this VPS hosts more than one app) that has an XSS or is attacker-controlled can post `{"enabled":true}` to the tracking switch, delete an event, create an org admin or revoke every link, with the officer's cookie attached. It also fails open on any browser that does not enforce SameSite. Cheap to close.
- Fix: In one place (a dependency or the existing middleware) refuse any state-changing `/api/setup/` request whose `Origin`/`Referer` host is not our own, or require `Content-Type: application/json` plus a custom header (`X-Requested-With`) which a cross-site form cannot send without a preflight. Prefix the cookie `__Host-` when secure.
- Effort: S

### SEC-5: Rejected packets from stations the roster does not know are written raw to the database, contrary to the documented "seen, never stored" rule for the area filter
- Severity: Medium
- File: src/courseops/ingest.py:178-190 (raw log happens before the roster check at :203-215)
- What: `handle_line` logs every packet that fails to parse or carries no position into `raw_packet` (with `event_id`) BEFORE deciding whether the sender is on the roster. With the area filter on, that is every status, message, telemetry and weather packet from the public within a mile of the course, including the text of APRS messages between third parties.
- Evidence: `if log_all_raw: db.log_raw_packet(conn, event_id, _now(), line, rejection.reason, rejection.detail); return None` at :186-190, ahead of `known = report.station_key in roster_keys` at :203. Reproduced: `STRANGER-9>APRS,TCPIP*:>status text private` with an empty roster produced a `raw_packet` row. The docstring three lines above says the opposite ("NOT stored and not logged - not even raw").
- Why it matters: CLAUDE.md: "Anything heard that the roster does not know is held in memory ... Not a position, not a raw packet, for anyone else." The area filter was accepted on that promise. The rows are in the nightly backups too, so they outlive the event. `raw_packet` also grows unbounded with everything the area delivers.
- Fix: Move the roster/base-callsign check ahead of the raw log for rejected packets (parse the `from` callsign even on `Rejected`, or only raw-log rejects whose source is rostered), and drop `log_all_raw` for the live server. Add a test with a non-roster status packet asserting `raw_packet` stays empty.
- Effort: S

### SEC-6: Role bearer tokens are written to logs on every request and on every restart
- Severity: Medium
- File: deploy/apache-courseops-ssl.conf:79 and deploy/apache-courseops.conf:149 (`CustomLog ... combined`); src/courseops/cli.py:654-663 (`cmd_serve` prints every role link); deploy/courseops.service:31 (`serve mankato2026` under systemd)
- What: Apache's `combined` format logs the full request path, which is `/e/<slug>/<token>`, `/api/<slug>/<token>/...` and `/ws/<slug>/<token>` for every field request. Separately, `courseops serve <event>` prints the five role URLs to stdout, which under systemd is journald, on every restart and every deploy.
- Evidence: `lines.append(f"  {access.ROLE_LABELS[role]:<14} {base}/e/{event['slug']}/{tokens[role]}")` then `print(...)`; `CustomLog ${APACHE_LOG_DIR}/courseops-access.log combined`.
- Why it matters: The whole access model is "the link is the credential". Those credentials now sit in `/var/log/apache2/*` (group `adm`, rotated copies kept) and in the journal, readable by anyone with `adm`/`systemd-journal` membership and by whatever log shipping is added later. Revoking a link does nothing about old log lines. uvicorn's own access log is off (`log_level="warning"`), which is right.
- Fix: In Apache, log a redacted path (e.g. `SetEnvIf Request_URI "^/(e|api|ws)/[^/]+/[^/]+" tokenreq` plus a `LogFormat` that omits `%r` for those, or `%U` with a `mod_rewrite` env that strips the third segment). In `cmd_serve`, print the links only when stdout is a TTY, or print `/setup` and tell the officer to read links there.
- Effort: S

### SEC-7: Username enumeration by timing - the "dummy hash" path makes an unknown username take twice as long, not the same
- Severity: Low
- File: src/courseops/users.py:398-401
- What: For a missing user the code computes `hash_password("dummy-for-timing")` (one scrypt) and then `verify_password(...)` on that fresh hash (a second scrypt). For an existing user only `verify_password` runs. The intent in the comment is defeated.
- Evidence: `stored = row["password_hash"] if row else hash_password("dummy-for-timing"); ok = verify_password(password or "", stored)`; measured 0.62 s (exists) vs 1.20 s (does not exist).
- Why it matters: A 2x gap survives any network jitter, so an outsider can confirm which club officers have accounts before guessing passwords. Low on its own because the usernames are few and the real problem is SEC-3.
- Fix: Keep a module-level pre-computed dummy hash (`_DUMMY = hash_password(secrets.token_hex(8))` at import) and verify against that, so both branches do exactly one scrypt.
- Effort: S

### SEC-8: Request bodies are read into memory without a cap - the unauthenticated login body and the authenticated file upload
- Severity: Medium
- File: src/courseops/web.py:1678-1687 (`_json_body`), :685-688 (login reads the body before any check), :813-821 (`payload = await file.read()`, then `tmp.write_bytes`)
- What: Starlette buffers the entire body for `request.json()`; nothing limits it. The import route reads the whole upload into RAM and to disk before `kml.load` applies its 32/64 MB caps.
- Evidence: `body = await request.json()`; `payload = await file.read()`.
- Why it matters: A multi-hundred-MB POST to `/api/setup/login` needs no credential and is enough to OOM a small VPS (uvicorn has no body limit; Apache's default `LimitRequestBody` is 0/1 GiB depending on version and is not set in the shipped vhost). The frozen Windows build has no proxy in front at all.
- Fix: Set `LimitRequestBody 70000000` in the vhosts, and in the app check `Content-Length` (reject > 64 KB for JSON routes, > `MAX_KML_BYTES` for import) before reading; stream the upload to the temp file in chunks.
- Effort: S

### SEC-9: No Content-Security-Policy anywhere, and Leaflet is loaded from unpkg.com on every page
- Severity: Low
- File: src/courseops/static/index.html:29-30, 202-203; src/courseops/static/setup.html:12, 549; src/courseops/report.py:30-34; deploy/apache-courseops-ssl.conf:67-76 (headers set only there)
- What: The app sets no security headers itself; the SSL vhost adds `nosniff`, `Referrer-Policy` and `X-Frame-Options` but no CSP. Leaflet CSS/JS come from `unpkg.com` (with SRI, which is good) and the OSM tiles are the other third party.
- Evidence: `<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-..." crossorigin="">`.
- Why it matters: Escaping in both clients is consistent (see Clean), so this is defence in depth rather than a live hole. But CLAUDE.md's reason for shipping fonts ("every field phone reporting to a third party") applies equally to unpkg, and an unpkg outage on race morning means no map at all, on every phone. A CSP restricted to `'self'`, `unpkg.com` and `tile.openstreetmap.org` would also turn any future escaping slip into a blocked request instead of a token theft.
- Fix: Vendor `leaflet.js`/`leaflet.css`/its images into `static/` (one 150 KB file set) and add a CSP header in the app (middleware, so the Windows build gets it too): `default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; style-src 'self' 'unsafe-inline'` (Leaflet sets inline styles); drop `unsafe-inline` for scripts by moving the two inline `<script>` snippets into files.
- Effort: M

### SEC-10: The map page relies on the proxy for its referrer policy; without Apache nothing states one
- Severity: Low
- File: src/courseops/static/index.html:7-31 (no `<meta name="referrer">`); deploy/apache-courseops-ssl.conf:75
- What: The token is in the path and the page loads tiles from `tile.openstreetmap.org`. `Referrer-Policy: strict-origin-when-cross-origin` is set by Apache only. The Windows build and any LAN deployment get whatever the browser defaults to.
- Evidence: the head has `robots` and `viewport` metas but no `referrer` meta; the help link is `rel="noopener noreferrer"`.
- Why it matters: Current browsers default to `strict-origin-when-cross-origin`, so today this is a latent gap. It costs one line to make the app's own pages state the policy that CLAUDE.md spends a paragraph on.
- Fix: Add `<meta name="referrer" content="strict-origin-when-cross-origin">` to `index.html` and `setup.html`, and set the header in the app for every response.
- Effort: S

### SEC-11: Unvalidated JSON value types turn into 500s, and one of them poisons the map for every viewer
- Severity: Low
- File: src/courseops/admin.py:110-117 (`center_lat`/`center_lon`/`zoom` written raw); src/courseops/web.py:1360, 1376 (`int(body.get("token_id"))`), :1709-1714 (`body.get` on a non-dict), :1493-1496 (`int(organization_id)`, `[int(i) for i in ...]`), :1776 (`float(body.get("lat"))` on None -> TypeError is caught, OK)
- What: `update_event` stores whatever JSON value arrives for the centre and zoom (`"abc"`, a dict -> `sqlite3.ProgrammingError` 500). `token_id: null` -> `TypeError` 500. A JSON array body to the station-status route -> `AttributeError` 500. `event_ids: "12"` iterates characters.
- Evidence: reproduced `POST /api/setup/events/1 {"center_lat":"abc","zoom":{"a":1}}` -> 500; with a string alone it is stored and then served in `build_state` as `"center_lat": "abc"`.
- Why it matters: All admin-only, so not an attack surface for outsiders, but a string centre breaks `map.setView` for every volunteer's phone with no server error, and 500s from the `_guard`ed routes are tracebacks in the journal rather than a message the officer can act on.
- Fix: Coerce and range-check `center_lat`/`center_lon` with the existing `_coordinate`, `zoom` with `int()` in 1-20; wrap `int(...)` conversions in `_guard`; have `_json_body` be used by every JSON route (the station-status route has its own copy).
- Effort: S

### SEC-12: Dead authentication code - `admin_token` table and `resolve_admin` are unreferenced
- Severity: Low
- File: src/courseops/access.py:146-183; src/courseops/schema.sql:467-471
- What: `ensure_admin_token`, `resolve_admin`, `rotate_admin_token` and the `admin_token` table are never called from web.py or cli.py (grep shows no callers outside access.py). `users.purge_expired_sessions` (users.py:454) is also never called, so expired session rows only go away when individually hit.
- Evidence: `grep -rn resolve_admin src/` -> only the definition.
- Why it matters: A second, unreviewed credential path is exactly what gets wired back in by mistake; and a table that still exists in deployed databases invites someone to read it as live. Expired sessions are harmless but the table grows by one row per login forever.
- Fix: Delete the three functions; leave the table (dropping needs a migration) with a comment that it is retired; call `purge_expired_sessions` from `init_schema` or the lifespan.
- Effort: S

### SEC-13: Two GET routes mutate state
- Severity: Low
- File: src/courseops/web.py:1338-1351 (`GET .../links` calls `access.ensure_tokens`, which inserts); :1096-1137 (`GET .../categories` seeds and `conn.commit()`s)
- What: A GET creates access tokens for roles that have none, and another seeds taxonomy rows. Lax cookies ARE sent on cross-site top-level GET navigations, so these are the only setup routes a cross-site page can trigger with the officer's session.
- Evidence: `access.ensure_tokens(conn, event_id)` inside the GET handler.
- Why it matters: The effects are benign today (a missing role's link gets created), which is why this is Low; but "GET never writes" is the rule that keeps SEC-4's Lax-only defence meaningful, and the next side effect added here will not be benign.
- Fix: Create the missing tokens in `create_event` (already done) and on `POST .../links`; let the GET report what exists. Seed categories in `create_event`/`init_schema` only (already done there).
- Effort: S

### SEC-14: Layer colour and icon are accepted unvalidated server-side; the only guard is in the client
- Severity: Low
- File: src/courseops/categories.py:172-206, 222-262 (`color`, `icon` stored as given); src/courseops/static/app.js:889-891 (`cssColor` allow-list), src/courseops/static/icons.js:52-53 (`POI_GLYPHS[name] || POI_GLYPHS.pin`)
- What: Course colours go through `styling.is_valid_color`; layer colours and icon names do not. `app.js` interpolates the colour into `style="background:..."` and the icon into a lookup, and both are safe only because the client re-validates.
- Evidence: `values.append((payload.get("color") or "").strip() or None)` with no check; client `cssColor()` regex.
- Why it matters: The server is the boundary between admins and the field phones. Today a hostile admin can only make the pins grey; a future client (the report page, a native app) that trusts the server's colour would carry a CSS injection.
- Fix: Reuse `styling.is_valid_color`/`normalize_color` in `add_poi_category`/`update_poi_category`, and check `icon` against the glyph list (mirror `POI_GLYPHS` keys in Python or store the list once).
- Effort: S

### SEC-15: Deploy workflow interpolates the ref into a shell line and re-learns the host key on every run
- Severity: Low
- File: .github/workflows/deploy.yml:49, 62-63, 73-76
- What: `TAG="${{ github.event.inputs.tag || github.ref_name }}"` and the later `ssh ... "${{ secrets.DEPLOY_PATH }}/deploy/deploy.sh ${{ steps.target.outputs.tag }}"` are expression interpolations into `run:`; a tag named `v1$(cat ~/.ssh/id_deploy)` or a crafted dispatch input runs on the runner with the deploy key in `~/.ssh`. `ssh-keyscan` at deploy time pins whatever answers, so it is trust-on-first-use every run, not pinning.
- Evidence: the three lines cited; no `KNOWN_HOST` secret.
- Why it matters: Both need repository write access (to push a tag or dispatch), and the `production` environment can require approval, so this is not an outsider's route - but a contributor with write access should not be able to exfiltrate the server key through a tag name, and a DNS/BGP hijack at deploy time gets the key handed to it.
- Fix: Pass the ref through `env:` (`env: TAG: ${{ ... }}` then `"$TAG"`), validate it in the workflow with the same regex as the forced command, and store the server's host key in a secret written to `known_hosts` instead of `ssh-keyscan`.
- Effort: S

### SEC-16: Import leaves an empty temp directory behind per upload
- Severity: Low
- File: src/courseops/web.py:820-828
- What: `tempfile.mkdtemp()` creates a directory, the file inside is unlinked, the directory never is.
- Evidence: `tmp = pathlib.Path(tempfile.mkdtemp()) / f"upload{suffix}"` ... `tmp.unlink(missing_ok=True)`.
- Why it matters: `PrivateTmp` means these accumulate in the service's private tmp until restart; a club re-importing dozens of times is fine, a hostile admin looping is an inode leak. Trivial.
- Fix: `with tempfile.TemporaryDirectory() as d:` around the parse.
- Effort: S

### SEC-17: Assigning a staged point accepts a layer key that does not exist
- Severity: Low
- File: src/courseops/admin.py:166-176, src/courseops/importer.py:333-373
- What: `create_poi`, `update_poi` and `move_pois` call `categories.get_poi_category`; the review-screen path (`assign_features` kind `poi`) does not, so a place can land in a layer no row describes - CLAUDE.md says this is refused. Functional, not security; noted because it was found during the review.
- Evidence: `importer.assign_poi(conn, event_id, feature_id, poi_type, ...)` inserts `poi_type` directly.
- Why it matters: A pin with no layer is drawn nowhere and the club sees an assigned feature vanish.
- Fix: Call `categories.get_poi_category(conn, event_id, poi_type)` at the top of the `kind == "poi"` branch.
- Effort: S

### SEC-18: First-run bootstrap is open to whoever reaches /setup first
- Severity: Low
- File: src/courseops/web.py:637-683
- What: Until one user exists, `POST /api/setup/first-user` creates a system administrator for anyone. The double-submit race is handled; the window between "service is public" and "officer has signed up" is not.
- Evidence: `if users.any_users(conn): raise HTTPException(409, ...)` is the only gate.
- Why it matters: On the VPS the vhost is public the moment certbot finishes, and a deploy that recreates the database (restore gone wrong, wrong `DB_PATH`) reopens it silently. Low because it is a one-time window and the runbook has the officer do this straight away.
- Fix: Print a one-time setup code at startup when no users exist (`cmd_serve` already prints a first-run notice) and require it in the first-user form; or bind first-user creation to the CLI.
- Effort: S

## Unconfirmed

### SEC-19: Quadratic regex over a hostile KML `<description>`
- Severity: Low
- File: src/courseops/kml.py:186-189, 223
- What: `_ATTR_ROW` (`<td[^>]*>\s*([^<>]{1,60}?)\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>` with `re.S`) is applied to every description. A description of 60 MB consisting of `<td>x</td><td>` repeated with no closing tag makes each match attempt scan to the end - O(n^2) - inside the event loop. Not measured; authenticated admin only, and SEC-8/SEC-3's "sync work on the loop" fix covers it.
- Fix: Cap the description length passed to `attributes_from_description` (a real attribute table is a few KB) and run the import in a thread.
- Effort: S

## Clean
- SQL: every query in db.py, admin.py, users.py, access.py, incidents.py, leaders.py, categories.py, importer.py, report.py is parameterised; the f-strings build column lists from fixed tuples (`_ADDED_COLUMNS`, `create_event(**fields)` keys from admin.create_event, `allowed` dicts), never from input. `PRAGMA foreign_keys = ON` and `ON DELETE CASCADE` are in place for every event-scoped table.
- Event scoping of live-token routes: every `/api/{slug}/{token}/...` write goes through `require_capability` and passes `granted.event_id` into a query that filters on it (incidents, leaders, station status, ssid adopt/unbind/ignore, bib colour, station log, incident log). `access.resolve` joins the token to the slug, so a token is useless against another event. Invalid token -> 404; valid token lacking capability -> 403 (documented).
- Setup routes not named in SEC-1/SEC-2 are scoped: courses, POIs, roster, categories, roles, leaders, `set_poi_courses`, `reorder_*`, `move_pois`, `assign_station_to_poi` all carry `event_id` in the WHERE clause. `may_access_event` checks organisation before assignment (users.py:348-371) as documented; org admins cannot see other orgs' events, users or organisations; `may_manage_user` refuses system-admin targets; last-system-admin and self-delete guards hold.
- Password hashing: scrypt with per-hash salt and self-describing parameters, `hmac.compare_digest`, 10-char minimum; session tokens `secrets.token_urlsafe(32)`, stored server-side, revocable, cleared on password change and deactivation; cookie `HttpOnly`, `SameSite=Lax`, `Secure` behind `--behind-proxy` with `forwarded_allow_ips` pinned to the proxy; logout deletes the row; no session fixation path (the server never accepts a client-chosen id).
- Role tokens: `secrets.token_urlsafe(24)` (192 bits); DB equality lookup is not a practical timing oracle at that length.
- XSS: `app.js` and `setup.js` route every interpolated value through `escapeHtml`/`esc` including attribute contexts (`iconBtn`/`iconLink` escape attrs and labels); the two places that take a colour set `style.background` via the property or `cssColor()` allow-list; `glyphSvg` is a lookup; server messages land in `textContent` (`banner`). `report.py` escapes every field and JSON-escapes `<` inside the `<script type="application/json">` block; the timezone is read from an escaped element, not interpolated into script. `guides.py` escapes before emitting markup, page names are `^[a-z0-9][a-z0-9-]*$`, and the content is repo-authored.
- Path traversal: `/help/{page}` regex plus `INDEX` special-case; `StaticFiles` mounts for `/static` and `/help/images`; `_asset_version` uses `Path(name).name`.
- File upload: `defusedxml` is the only XML parser used (`kml.parse_kml_bytes`; gpx.py only walks the already-parsed tree); KMZ has a declared-size cap, a 200x ratio cap and the 32 MB archive cap, and Python's `ZipExtFile` bounds decompression to the declared size; the KMZ member is chosen by name and read in memory, never extracted to disk.
- WebSocket: authenticates by token before `accept()`, subscribes with the role's capabilities, `hub.publish(..., requires=CAP_SSID / CAP_INCIDENT_REPORT)` is honoured, per-subscriber queue bounded at 64 with drop-not-block, and Staff never receives incidents or nearby. The server never reads client frames, so there is no client-message parsing surface.
- Privacy: nearby stations are memory-only, capped at 200, sent only to `CAP_SSID`, and removed once adopted/ignored (except the raw-log gap in SEC-5); the report page carries no names; the viewer's own fix is never posted except by the explicit "Here" button; `/healthz` returns exactly `status` and `version`; the commit id is behind the session.
- Ingest: passcode fixed at `-1` from the environment, login line never logged, `readline` bounded by asyncio's 64 KB limit, backoff with jitter, one connection enforced by `_start_ingest`, enable refused without a usable callsign.
- Deploy: `ssh-deploy-command.sh` accepts only `^[A-Za-z0-9][A-Za-z0-9._/-]{0,60}$` and execs `deploy.sh` with a quoted argument; sudoers is three exact `systemctl` commands with absolute paths; `backup.sh` makes the directory 700 and files 600 and uses `.backup` rather than `cp`; the unit runs unprivileged with `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`; no secret is echoed in any workflow; the release workflow refuses a tag that disagrees with the package version.
- Open redirects: the only redirect is `/` -> `/setup`, fixed.
- Headers: `X-Content-Type-Options`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: SAMEORIGIN` are set in the SSL vhost as documented; HSTS off is a documented decision.
