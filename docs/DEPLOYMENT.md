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

## The short way: a tunnel

If you are standing something up for one event rather than running a server all
year, you do not need Apache, DNS or certbot. A tunnel gives you a public HTTPS
address pointing at the app on your own machine, and it hands you a certificate
for free. This is the least effort path to a working location dot.

Whichever you use, the tunnel terminates TLS and speaks plain HTTP to the app
on localhost, so **two flags matter**:

```bash
courseops serve <event> --behind-proxy --base-url https://<your-tunnel-host>
```

- `--behind-proxy` - without it the app sees "http", and admin session cookies
  silently lose their `Secure` flag in the one deployment where it matters.
- `--base-url` - so the role links it prints carry the tunnel address rather
  than `localhost`, which is what you are about to send to volunteers.

The default `--trusted-proxy 127.0.0.1` is already correct: all of these agents
run on the same machine and connect over the loopback.

### Tailscale

If your club already uses Tailscale this is the least work of all, and it comes
in two flavours that answer two different questions.

```bash
tailscale serve 8000     # HTTPS, visible to your tailnet only
tailscale funnel 8000    # HTTPS, visible to the whole internet
```

**`serve` is the one for testing.** Your own phone gets a real HTTPS address, so
geolocation works and you can walk the course with it, while nothing is exposed
beyond your own devices. That is the gap the LAN setup cannot close.

**`funnel` is for the event**, when volunteers who are not on your tailnet need
in. Funnel's public side must use port 443, 8443 or 10000.

### Cloudflare Tunnel or ngrok

Both work the same way and neither needs a domain for a quick tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
ngrok http 8000
```

They print a random public hostname; pass it to `--base-url`. Check whether your
plan shows an interstitial page before the site - a volunteer meeting one on
race morning will assume the link is broken.

### What a tunnel does not change

Anyone holding a role link can use it, and now from anywhere rather than from
your wifi. That is already the access model - the links are bearer tokens - but
a public tunnel widens the audience from "people in the building" to "people who
have the URL", so treat a leaked link accordingly and revoke it with
`courseops revoke-link`. Nothing else about the security model changes: there is
still no public view, and an invalid token still gets a 404.

Tunnels are also a dependency you do not control on race morning. For a single
event that is usually an acceptable trade against configuring Apache; for a club
running this every year, the rest of this document is the sturdier answer.

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

None of these block a single club's first event. All three are tracked as
issues, and each one is triggered by **hosting a second organization**, not by
running the first.

| Issue | What it is | When it matters |
|---|---|---|
| [#3](https://github.com/MikeWills/CourseOps/issues/3) | OSM's tile policy does not cover a service hosted for many clubs. One URL to change, but the map style affects legibility, so the palette needs rechecking. | Before a second organization shares the box |
| [#4](https://github.com/MikeWills/CourseOps/issues/4) | Backups are whole-database, so restoring one club rolls back the others, and a backup contains every club's data. | Before a second organization, or before offboarding one |
| [#5](https://github.com/MikeWills/CourseOps/issues/5) | Resource limits, a signup path (including password reset, which needs email), and static asset caching after an update. | When it is offered rather than run |
