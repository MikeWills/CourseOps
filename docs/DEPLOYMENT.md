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

### Which one

|  | Account needed | Volunteers install | Address survives a restart | Click-through |
|---|---|---|---|---|
| **Tailscale Funnel** | free, host only | nothing | **yes** | no |
| **Tailscale Serve** | free, host + viewers | Tailscale | yes | no |
| **Cloudflare quick tunnel** | **none** | nothing | no | no |
| **ngrok**, free | free | nothing | one dev domain | **yes** |
| **ngrok**, paid | $8-10/mo | nothing | yes | no |

Two columns decide it in practice.

**"Address survives a restart"** is the one people underestimate. A Cloudflare
quick tunnel gets a new random hostname every time it starts, so if the laptop
reboots at mile 6, every link you handed out is dead and you are re-sending
fifteen URLs while running a net. Issue links only after the tunnel is up, and
know that a restart means re-issuing them.

**"Volunteers install"** is why Funnel beats Serve for the event itself. Serve
publishes to your tailnet, so everyone who needs the map has to be on it -
fine for you and a second operator, a poor ask for fifteen volunteers on race
morning. Funnel puts it on the public internet, where a link is just a link.

So: **Serve while you are building and testing** on your own phone, **Funnel on
the day**. The first time you run `tailscale funnel` it may send you to the
admin console to enable Funnel for the tailnet; do that before race week, not
during it.

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

### Cloudflare Tunnel

Free, no click-through, and no domain needed for a quick tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a random `trycloudflare.com` hostname; pass it to `--base-url`. The
hostname changes every time you restart, so issue the links after you start the
tunnel, not before. A named tunnel gives you a stable hostname and is also free,
but needs a domain you control.

### ngrok, with a caveat that matters

ngrok works, but **check which plan you are on before handing links to
volunteers.** The free tier puts an interstitial warning page in front of all
HTML browser traffic: every volunteer gets a click-through telling them the site
is served by ngrok before they reach the map. Clicking through sets a cookie
that suppresses it for seven days, so you will stop seeing it long before they
do - which is how this ships by accident.

At 6am in a car park, a page like that reads as a phishing warning, and the
volunteer's reasonable conclusion is that the link is broken. The free tier also
caps requests and transfer, which a live map with a dozen phones on it can
reach. A paid plan removes the interstitial.

If ngrok is already part of your toolkit and you pay for it, use it:

```bash
ngrok http 8000
```

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

## On a Raspberry Pi

Nothing in this needs a real server. It is one Python process, one SQLite file
and a frontend with no build step, so a Pi runs it comfortably - which for a
club is often the honest answer: a box that lives in the shack, costs nothing to
leave on, and can go to the event in a bag.

Follow the ordinary install below; there is no separate Pi path. Only one thing
in the dependency tree contains compiled code (`pydantic-core`, via FastAPI) and
it publishes wheels for both `aarch64` and `armv7l`, so nothing builds from
source on the Pi. Raspberry Pi OS Bookworm ships Python 3.11, which is the
version this needs. **64-bit is the safer choice** if you are installing fresh.

Two Pi-specific cautions, neither about performance:

- **The SD card is the weak point, not the CPU.** SQLite runs in WAL mode, which
  survives a crash, but a card that is failing takes the event's only record
  with it. Boot from a USB SSD if you have one, and take the backup in section 5
  seriously.
- **It still needs internet.** APRS-IS is an internet feed, so a Pi at a start
  line needs a hotspot or a cell modem. Being on the course does not help it
  hear anything.

---

## 1. Install

This section is the **first-time** setup. Once it is done, updates are
`deploy/deploy.sh` (section 4a) and you never repeat these steps.

### Letting the server read a private repository

The repository is private, so the server needs its own read access before it
can clone or fetch anything. As the `courseops` user, make a key and give
GitHub the public half:

```bash
sudo -u courseops ssh-keygen -t ed25519 -f /opt/courseops/.ssh/id_github -N ""
sudo -u courseops cat /opt/courseops/.ssh/id_github.pub
```

Paste that into the repository under **Settings -> Deploy keys -> Add deploy
key**, and **leave "Allow write access" unticked** - the server only ever reads.

Then tell git to use it:

```bash
sudo -u courseops tee /opt/courseops/.ssh/config >/dev/null <<'EOF'
Host github.com
    IdentityFile /opt/courseops/.ssh/id_github
    IdentitiesOnly yes
EOF
sudo -u courseops chmod 600 /opt/courseops/.ssh/config
sudo -u courseops ssh -T git@github.com   # expect "successfully authenticated"
```

> **Two different keys, and it is worth keeping them straight.** This one lets
> the **server read GitHub**, and is a *GitHub deploy key*. The one in section
> 4a lets **GitHub reach the server**, and is an *SSH key in your repository
> secrets*. They point in opposite directions and neither substitutes for the
> other. If you only ever deploy by hand you still need this one; you do not
> need that one.

```bash
sudo useradd --system --home /opt/courseops --shell /usr/sbin/nologin courseops
sudo mkdir -p /opt/courseops
sudo chown courseops:courseops /opt/courseops

sudo -u courseops git clone git@github.com:YOURNAME/CourseOps.git /opt/courseops
cd /opt/courseops
sudo -u courseops python3 -m venv .venv
sudo -u courseops .venv/bin/pip install -e .

sudo -u courseops cp .env.example .env
sudo -u courseops nano .env          # set APRS_CALLSIGN; leave the passcode -1
```

The database creates itself on first start - there is no separate setup step.

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

## 4a. Deploying from GitHub

Pushing a version tag builds the release and deploys it to the server over SSH.
Tags rather than merges: `main` stays free for work in progress, what is running
is always a version you can name, and rolling back is deploying the previous tag
instead of an archaeology exercise.

The script that does the work is [`deploy/deploy.sh`](../deploy/deploy.sh).
**There is nothing to upload** - it arrives with the clone in section 1, so it
is already at `/opt/courseops/deploy/deploy.sh`. It updates an install that
already exists; it does not replace the first-time setup above.

It runs perfectly well by hand, and running it by hand once is how you should
prove it before letting a workflow do it unattended:

```bash
sudo -u courseops /opt/courseops/deploy/deploy.sh v0.1.0
```

If that works, automating it is only a matter of adding the secrets below. If it
does not, you have found the problem at a keyboard rather than at 03:00.

It backs the database up with `.backup` first, keeps the last ten of those,
installs the tag, restarts the service, and then **verifies `/healthz` and rolls
back to the previous commit if it does not answer**. Nobody is watching a deploy
at 03:00, and a half-working one that leaves the app down until somebody notices
is worse than one that refuses.

### The deploy key

Make a key that exists only for this, on your own machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/courseops_deploy -C "github-deploy" -N ""
```

Put the **public** half on the server, restricted so that a stolen key cannot be
used as a shell. `command=` means this key can run exactly one thing regardless
of what the client asks for:

```bash
# /home/courseops/.ssh/authorized_keys  (or wherever that user's home is)
command="/opt/courseops/deploy/deploy.sh ${SSH_ORIGINAL_COMMAND##* }",\
no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA... github-deploy
```

The deploy needs to restart the service, which needs root. Give it that one
command and nothing else:

```bash
# sudo visudo -f /etc/sudoers.d/courseops
courseops ALL=(root) NOPASSWD: /bin/systemctl restart courseops
```

### Repository settings

Under **Settings -> Secrets and variables -> Actions**, as *secrets*:

| Secret | Value |
|---|---|
| `SSH_HOST` | the server's hostname or IP |
| `SSH_USER` | `courseops` |
| `SSH_KEY` | the **private** half of the key above |
| `SSH_PORT` | only if sshd is not on 22 |
| `DEPLOY_PATH` | only if the install is not at `/opt/courseops` |

These names match the ones used by the other projects here deliberately - one
convention to remember rather than a per-project dialect.

And as a *variable* (not a secret - it is public anyway):

| Variable | Value |
|---|---|
| `PUBLIC_URL` | `https://courseops.example.org`, to check from outside |

Finally create an **Environment** named `production` under Settings ->
Environments. That is where the secrets live, and it is what lets you require a
click before anything touches the server. Worth having even alone: a deliberate
approval before deploying to a box a club depends on costs one second.

### What it checks

The script checks `/healthz` from inside the box, which answers "does the app
work" - it opens the database rather than only confirming the process is up,
because those are different claims and a deploy that proves only the first will
leave a broken version running.

The workflow then checks `PUBLIC_URL/healthz` from outside, which is a different
question again: it exercises Apache, TLS and DNS, and catches the case where the
app is perfectly healthy and nobody can reach it. A failure there is *not* rolled
back automatically, because the app is fine - the proxy is not.

---

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
