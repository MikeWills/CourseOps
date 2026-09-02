# Deployment

Course Ops behind Apache with a Let's Encrypt certificate. Templates live in
[`deploy/`](../deploy).

The shape: **uvicorn listens on 127.0.0.1 only, Apache terminates TLS and proxies
to it.** Binding loopback means the only way in is through the proxy, so nobody
can reach the app on its port and bypass TLS.

---

## Why HTTPS is not optional here

Browsers block the Geolocation API outside a secure context. Over plain HTTP the
"where am I" dot simply does not work for the Liaison and Logistics roles on
their phones — and those are the roles the mobile design exists for.

Everything else degrades gracefully without TLS. That one feature does not.

---

## 1. Install

```bash
sudo useradd --system --home /opt/courseops --shell /usr/sbin/nologin courseops
sudo mkdir -p /opt/courseops
sudo chown courseops:courseops /opt/courseops

sudo -u courseops git clone <repo-url> /opt/courseops
cd /opt/courseops
sudo -u courseops python3 -m venv .venv
sudo -u courseops .venv/bin/pip install -e .

sudo -u courseops cp .env.example .env
sudo -u courseops nano .env          # set APRS_CALLSIGN; leave the passcode -1
sudo -u courseops .venv/bin/courseops init-db
```

`APRS_PASSCODE` stays `-1`. That is read-only access to APRS-IS, and the app
never transmits — there is no reason for a real passcode to exist on the server.

Check it runs before involving Apache:

```bash
sudo -u courseops .venv/bin/courseops serve --no-ingest --port 8000
curl -I http://127.0.0.1:8000/setup      # expect 200
```

## 2. Apache

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel headers rewrite ssl
sudo cp deploy/apache-courseops.conf /etc/apache2/sites-available/courseops.conf
sudo nano /etc/apache2/sites-available/courseops.conf     # set your domain
sudo a2ensite courseops
sudo apache2ctl configtest && sudo systemctl reload apache2
```

### The WebSocket rules are the part that breaks

The live map is fed by a WebSocket at `/ws/`. With a plain `ProxyPass`, the
upgrade never completes and **the map loads correctly and then never moves**,
with no error anywhere obvious. `mod_proxy_wstunnel` plus the rewrite in the
template is what prevents that, and those rules must come *before* the catch-all
`ProxyPass` or they never match.

Verify it explicitly rather than assuming — see [Checking it worked](#4-checking-it-worked).

## 3. Certificate

```bash
sudo certbot --apache -d courseops.example.org
```

Certbot rewrites the `:443` block and installs a renewal timer. Confirm the
renewal path works, because a silent failure surfaces 90 days later:

```bash
sudo certbot renew --dry-run
systemctl list-timers | grep certbot
```

Note the `:80` vhost keeps `/.well-known/acme-challenge/` unproxied. Without
that exclusion renewal fails, because the challenge would be forwarded to the
app, which knows nothing about it.

## 4. Checking it worked

```bash
sudo cp deploy/courseops.service /etc/systemd/system/
sudo nano /etc/systemd/system/courseops.service     # set the event slug
sudo systemctl daemon-reload
sudo systemctl enable --now courseops
sudo journalctl -u courseops -f
```

Then check each thing that can fail independently:

| Check | Command | Expect |
|---|---|---|
| App is up | `curl -I https://your.domain/setup` | `200` |
| Not reachable directly | `curl -I http://your.domain:8000/setup` | refused |
| HTTP redirects | `curl -I http://your.domain/setup` | `301` to https |
| **WebSocket upgrades** | see below | `101 Switching Protocols` |
| Cookie is Secure | sign in, inspect `Set-Cookie` | contains `Secure` |

The WebSocket check, which is the one worth doing by hand:

```bash
curl -i -N -o - \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "https://your.domain/ws/<event>/<a real token>"
```

`101 Switching Protocols` means it works. Anything else — `200`, `400`, `502` —
means the map will load and then sit frozen.

Simplest end-to-end check: open the NCS link on a phone, confirm the badge top
right reads **Live** in green, and confirm the ◎ button places a blue dot. If
the badge says *Reconnecting*, the WebSocket is not proxied. If the location dot
reports *needs HTTPS*, the forwarded scheme is not reaching the app.

## 5. Backups

The SQLite file is the entire record — positions, incidents, status history,
accounts.

```bash
sudo -u courseops sqlite3 /opt/courseops/data/courseops.sqlite3 \
    ".backup '/opt/courseops/data/backup-$(date +%F).sqlite3'"
```

Use `.backup`, not `cp`: the database runs in WAL mode and a plain copy taken
mid-write can be inconsistent. Back up **before and after** each event.

---

## Still open

Two things this deployment does not solve, both carried from the plan's
known-gaps list.

### OpenStreetMap tile policy

The map uses OSM's public tile servers. Their usage policy covers a club-scale
deployment but **not a service hosted for many clubs** — that needs its own tile
source or a commercial provider (Thunderforest, MapTiler, Stadia and others all
serve this shape of traffic). It is a single URL in `static/app.js`.

This becomes a real obligation the moment more than one club is on the box, not
a nicety.

### Multi-tenant hosting

Organizations already isolate clubs within one install, so hosting several is
supported. Not yet addressed:

- **Backups per organization** — the current backup is all-or-nothing.
- **Resource limits** — one club importing a very large course affects everyone
  on the box.
- **A signup path** — organizations are created by a system administrator by
  hand, which is fine for a handful and not beyond that.

### Static asset caching

Browsers cached `index.html` across an update during development. After
deploying a new version, a hard refresh may be needed. Worth adding a version
query string or cache headers before this is updated often.
