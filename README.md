# retrace

A self-hosted replacement for Google Maps Timeline, running on your own server,
reachable through your existing Cloudflare Tunnel.

The point of it is accuracy. **Google throws your raw fixes away** — it
downsamples, snaps to roads and known places, and hands back an already-interpreted
result you cannot re-derive or argue with. Here every fix your phone ever sent is
kept forever, and everything above it is a *derived layer* that can be thrown away
and rebuilt from scratch with different settings. When a stay looks wrong you can
open it, look at the individual fixes and their accuracy circles, change a
threshold and rebuild.

Python 3.12, FastAPI, SQLite, Leaflet. No Node, no Docker, no build step.

---

## Layout

```
app/          the service
  db.py         schema and connections
  ingest.py     accepting fixes
  quality.py    flagging bad ones (never deleting them)
  segment.py    turning fixes into stays and trips
  places.py     turning stays into places
  timeline.py   assembling a day
  breakdown.py  resolving a day's overlapping streams into one partition
  providers/    one file per phone app; add a file, not a branch
static/       the web UI — ES modules, no bundler
  js/layout.js  block geometry: clustering and packing. Pure, no DOM
  js/track.js   the zoomable day: lanes, blocks, scrubbing
  js/minimap.js the place/trip rail left of the ruler, mirroring the track's pan/zoom
  js/mapview.js Leaflet: track, stays, raw fixes, areas
  js/daynav.js  the day bar, shared by both pages
  js/breakdown.js the two-ring donut on /breakdown
scripts/      backup, synthetic data
deploy/       systemd units
macos/        the MacBook activity daemon — runs on the Mac, not this server
tests/
data/         the database and its backups (gitignored)
```

---

## Install

```bash
cd /home/rohan/tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
python3 -c "import secrets; print('INGEST_TOKEN=' + secrets.token_urlsafe(32))"
# paste that into .env, replacing INGEST_TOKEN=replace-me
```

`.env` holds the shared secret your phone sends. It is gitignored; never commit it.

Run it:

```bash
deploy/install.sh
```

Checks `.env` and the venv are actually set up, installs and starts
`retrace.service` and `retrace-backup.timer`, and puts `.env` and `data/` back
to owner-only permissions — they hold the ingest token and every fix ever
recorded. If `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are set in `.env`, it
also installs and starts `retrace-whoop.timer` (see [WHOOP](#whoop)). Safe to
re-run any time the unit files change. `deploy/uninstall.sh` stops and removes
them again, leaving the repo, `.env` and `data/` untouched.

It listens on `127.0.0.1:8420` only. Nothing reaches it except through the tunnel.

Claude has scoped, passwordless sudo access to `sudo systemctl restart retrace`
in this repo (`.claude/settings.local.json` + `/etc/sudoers.d/rohan-retrace`) and
can restart the service itself after making code changes, without asking first.

---

## Cloudflare — the blocking step

With Tailscale deliberately out of the picture, **the phone cannot reach the
server at all until this is done.** The tunnel here is token-based and managed
from the dashboard, so none of it can be scripted locally.

### 1. Route the hostname

Zero Trust → **Networks → Tunnels** → your tunnel → **Public Hostname** → Add:

| | |
|---|---|
| Subdomain | `tracker` |
| Domain | your domain |
| Service | `HTTP` → `localhost:8420` |

Then put that hostname in `.env` as `PUBLIC_HOSTNAME=tracker.<your-domain>` and
restart. Requests arriving under any other name get a 400, which is what stops a
web page that resolves its own hostname to `127.0.0.1` from reading the API out
of a browser running on the server — a request like that never goes near
Cloudflare, so nothing above this layer would see it. `localhost` and
`127.0.0.1` are always allowed, so local testing and `scripts/inspect_page.py`
address it as usual. Leave it empty and the check is not installed at all.

### 2. Protect the UI

**Zero Trust → Access controls → Applications → Create new application →
Self-hosted and private → Add public hostname:**

| | |
|---|---|
| Domain | `tracker.<your-domain>` (no path) |
| Policy | **Allow**, Include → Emails → your email |

### 3. Let the phone in — with its own edge-enforced credential

Your phone cannot complete an SSO login, so it authenticates with a **Service
Auth** policy instead of a human one. It's still edge-enforced — Cloudflare
rejects a request missing the right credential before it ever reaches your
tunnel — just checking a machine token instead of an identity:

1. **Access controls → Service credentials → Service Tokens → Create Service
   Token.** Name it per device (`tracker-phone`, and later `tracker-macbook`,
   `tracker-shortcuts`, …) — separate tokens mean a lost device revokes cleanly
   without touching the others. Save the Client ID and Client Secret; the secret
   is shown once.
2. **Create new application** (same flow as above), domain `tracker.<your-domain>`
   **path** `/api/v1/locations` → policy **Service Auth**, Include → Service
   Token → the one(s) you created.
3. The client sends the credential as two headers, `CF-Access-Client-Id` and
   `CF-Access-Client-Secret` — see the OwnTracks `httpHeaders` field below.

**`INGEST_TOKEN` stays on too**, as an independent second check: Service Auth
stops a stranger who finds the URL; the app's own token stops anything that
reaches the app directly, including a future Cloudflare-side misconfiguration.
Cheap to keep, and it's already built.

> **Why the API is shaped the way it is.** Even with Service Auth, matching stays
> path-based, not method-based — so `/api/v1/locations` accepts `POST` and
> nothing else, and returns 405 to a GET, rather than trusting the Access layer
> alone to keep reads out. Raw fixes are read back from `/api/v1/points`, gated
> by the Allow policy like everything else.

Check it from anywhere:

```bash
# No credentials — Cloudflare's own 403, never reaches your server
curl -i https://tracker.<your-domain>/api/v1/locations -X POST -d '{}'

# Service token headers but no INGEST_TOKEN — reaches the app, gets its 401
curl -i https://tracker.<your-domain>/api/v1/locations -X POST -d '{}' \
  -H "CF-Access-Client-Id: <client_id>" \
  -H "CF-Access-Client-Secret: <client_secret>"
```

`journalctl -fu retrace` should show nothing for the first request (Cloudflare
never forwarded it) and a `401` for the second.

---

## iPhone — OwnTracks

App Store → OwnTracks → Settings:

| Setting | Value |
|---|---|
| Mode | **HTTP** |
| URL | `https://tracker.<your-domain>/api/v1/locations` |
| Authentication | on |
| Username | anything, e.g. `phone` — the server ignores this field |
| Password | your `INGEST_TOKEN` |
| httpHeaders | `CF-Access-Client-Id:<id>\nCF-Access-Client-Secret:<secret>` — one line, literal `\n` between the two (this field doesn't accept a real line break) |
| DeviceID | anything, no spaces, e.g. `phone` |
| Monitoring | `2` — the field is a raw integer: `-1` Quiet, `0` Manual, `1` Significant, `2` **Move**. `1` looks plausible but is the wrong mode; it uses iOS's coarse significant-change API instead of the displacement/interval settings below |
| locatorDisplacement | `25` m |
| locatorInterval | `180` s |
| Passphrase | **leave empty.** If set, OwnTracks encrypts every payload including location fixes as `{"_type":"encrypted",...}`. The server doesn't decrypt — encryption on top of HTTPS + Service Auth + the ingest token is redundant — so it silently discards them as a recognised-but-ignored type ([`app/providers/owntracks.py`](app/providers/owntracks.py)). You get 200s and nothing stored, no error anywhere |

Then, in **iOS Settings → OwnTracks**:

- Location: **Always**, and **Precise Location on**
- **Background App Refresh on**
- **Never force-quit the app.** iOS will not restart it for you, and tracking
  simply stops.

Low Power Mode suppresses background location. You will see gaps; the `batt` and
`bs` fields recorded with every fix are what let you tell "I was somewhere with no
signal" apart from "my phone was dead".

**Distance filter first, time heartbeat second** — never pure time-based:

| Mode | Displacement | Heartbeat | Points/day | Battery |
|---|---|---|---|---|
| High fidelity | 10–15 m | 60 s | 15–30k | +25–40 %/day |
| **Balanced ← default** | **25 m** | **3 min** | **3–6k** | **+10–15 %/day** |
| Battery saver | 100 m | 10 min | 0.5–1.5k | +5 %/day |

Start dense. Data can be thinned later; it can never be recovered. At balanced
that is roughly **1.1 GB/year** with full raw payloads retained.

---

## iPhone — Shortcuts

Personal Automations for device-activity signals, posted to the same ingest
path as OwnTracks. The credentials live in **one** shortcut that every
automation calls, rather than being copied into each one — adding an app is
then two automations with no secrets in them.

**The shared shortcut**, once. Shortcuts app → **+** → name it `Post Event`,
and give it four actions:

1. **Get Dictionary from Input**
2. **Get Value for Key** `subject` — rename its output `Subject`
3. **Get Value for Key** `value` — rename its output `Value`
4. **Get Contents of URL**:

| Field | Value |
|---|---|
| URL | `https://tracker.<your-domain>/api/v1/locations?format=shortcuts` |
| Method | `POST` |
| Headers | `Authorization: Bearer <INGEST_TOKEN>`, `CF-Access-Client-Id: <id>`, `CF-Access-Client-Secret: <secret>` |
| Request Body | JSON — `kind` `app`, `subject` the `Subject` variable, `value` the `Value` variable, `device` your phone's name |

The `Authorization` header is not optional: Service Auth gets the request past
Cloudflare, but the app checks the ingest token independently and returns 401
without it.

**The automations**, two per app. **Automation** tab → **+** → pick a trigger →
**Run Immediately**, **Notify When Run** off, then two actions: a **Dictionary**
with `subject` and `value` set for that signal, and **Run Shortcut** → `Post
Event` with the dictionary as its input. Duplicate a pair to cover another app
and change only the trigger and the `subject` string.

Each signal is a start/end pair, one automation per half; the server pairs
sequential pings into a range keyed on `(device, kind, subject)`:

| Automation | `kind` | `value` | `subject` |
|---|---|---|---|
| App → Is Opened / Is Closed (one pair per app you track) | `app` | `open` / `close` | the app's name |
| Wi-Fi → Connects / Disconnects (one pair per network you track) | `wifi` | `connected` / `disconnected` | the SSID |
| CarPlay → Connects / Disconnects | `carplay` | `connected` / `disconnected` | — |

`kind` is fixed to `app` inside `Post Event`. For the Wi-Fi and CarPlay rows,
either add a `kind` key to the dictionary and read it with a fourth **Get Value
for Key**, or keep a second copy of the shortcut per kind.

The body `Post Event` ends up sending for "Spotify → Is Opened":

```json
{"kind": "app", "subject": "Spotify", "value": "open", "device": "iphone"}
```

`device` identifies which phone sent it, so the day view can tell two
devices' activity apart when they overlap. `ts` is optional — omitted, the
server uses its own receive time, which is accurate enough for this kind of
log.

A signal missing its other half shows in the day view open-ended (still
connected/open) or as a flagged point with no duration (a close with no
matching open) — worth checking occasionally to see how often an automation
fails to fire its other half.

---

## MacBook

A small event-driven daemon in [`macos/`](macos/) posts the same event
shape Shortcuts does, from a LaunchAgent instead of a personal automation —
see [`macos/README.md`](macos/README.md) for the full build and install
spec. This section covers what it sends and why, once it's running.

Three signals, all posted to `/api/v1/locations?format=shortcuts` like the
phone:

| Signal | `kind` | `value` | `subject` |
|---|---|---|---|
| Screen unlock/wake vs. lock/sleep | `session` | `unlock` / `lock` / `heartbeat` | — |
| Frontmost app changes | `focus` | `start` | the app's name |
| Active tab's site, while a tracked browser is frontmost | `site` | `start` (`end` only when the browser loses focus) | bare domain e.g. `github.com`, or `incognito` / `no tab` |

Only one app is ever frontmost and only one site is ever current, so both
signals send just a `start` ping on each change — the server infers the
previous one's end from it, rather than the daemon sending an explicit `end`
for something it already knows is over. `site` is the one exception: leaving
the browser entirely has no next `site` ping to infer a boundary from, so
that transition still gets an explicit `end`.

`session` gets a `heartbeat` every minute while unlocked, from a separate
timer thread. `unlock`/`lock` are each already a clean transition on their
own, so this isn't for pairing — it's so a crash, dead battery, or lost
network that leaves an `unlock` with no matching `lock` doesn't stay
`ongoing` forever. Once heartbeats for a device stop arriving, the server
closes that range at the last one it got instead of leaving it open-ended.

Site-tracking covers Chrome, Brave, Edge and Arc — the Chromium family
exposes a `mode` property (`"normal"`/`"incognito"`) via AppleScript.
Incognito windows aren't skipped, they're recorded as their own subject:
browsing privately posts `site`/`start` with subject `incognito` rather than
a domain, so no URL is ever logged, but the fact of private browsing, and
its duration, is. `no tab` covers the same case for a front window with
nothing scriptable to read, e.g. a downloads popup. Safari and Firefox
aren't covered — Safari has no reliably scriptable way to detect a private
window, and Firefox has no AppleScript dictionary at all.

Unlike the iPhone, which needs one Shortcuts automation per app it tracks,
the Mac daemon observes every focus change through a single macOS
notification — no per-app setup. It's event-driven throughout (real
`NSWorkspace` and screen-lock notifications, not a polling loop) except for
the active browser tab, which has no "changed" notification and so is
polled every few seconds, but only while a tracked browser is actually the
frontmost app.

It needs its own Cloudflare Service Token, same as the phone — this is the
`tracker-macbook` token the Service credentials step above already
anticipated by name.

Sanity-check the path before installing the LaunchAgent for real:

```bash
curl -X POST "https://tracker.<your-domain>/api/v1/locations?format=shortcuts" \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>" \
  -H "Content-Type: application/json" \
  -d '{"kind":"focus","subject":"Terminal","value":"start","device":"macbook"}'
```

---

## WHOOP

[`scripts/whoop_sync.py`](scripts/whoop_sync.py) pulls nightly sleep from the
WHOOP API and posts it the same way Shortcuts does, run by a systemd timer on
this machine rather than by a phone or laptop:

| Signal | `kind` | `value` | `subject` |
|---|---|---|---|
| Main sleep (naps skipped) | `sleep` | `start` / `end` | — |

Only duration is synced — WHOOP's own `start`/`end` for each scored main
sleep, which is "time in bed" rather than the stricter stage-summed "time
asleep" the WHOOP app shows. Recovery, Strain and Workout aren't synced.

**1. Register an app.** [WHOOP Developer
Dashboard](https://developer-dashboard.whoop.com/apps/create) → Create App:

| Field | Value |
|---|---|
| Redirect URI | `http://localhost:8421/callback` |
| Scopes | `offline`, `read:sleep` |
| Privacy Policy URL | Not reviewed for an unpublished personal-use app — a throwaway URL or a one-line public Gist both work |

Save the Client ID and Client Secret into `.env` as `WHOOP_CLIENT_ID` and
`WHOOP_CLIENT_SECRET`.

**2. Authorize once, interactively.** WHOOP requires a one-time OAuth consent
in a real browser — there's no way around it for member-scoped health data,
even with a client ID and secret in hand. If this server is remote, forward
the callback port first so `localhost:8421` in your browser reaches this
machine's loopback instead of your own:

```bash
ssh -L 8421:localhost:8421 <host>
```

Then, on the server:

```bash
.venv/bin/python scripts/whoop_sync.py auth
```

Open the printed URL, approve access, and it saves a token pair to
`data/whoop_token.json` (mode 0600). Refresh tokens rotate on every use, so
this file changes on every sync from here on — that's expected, not a sign
of anything wrong.

**3. Sanity-check a sync by hand:**

```bash
.venv/bin/python scripts/whoop_sync.py
```

Prints a one-line summary (nights seen, events pushed) and hits
`127.0.0.1:8420` directly — no Cloudflare Access headers needed, since this
runs on the same machine as the server rather than a phone or laptop reaching
in through the tunnel.

**4. Install the timer**, once a manual sync looks right:

```bash
deploy/install.sh
```

`deploy/install.sh` installs and enables `retrace-whoop.timer` alongside the
other units whenever `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are set in
`.env` (steps 1 and 2 above). It runs at 8am and 10am server-local time,
re-fetching a rolling 3-day window each time; re-sent sleep records are
silently deduplicated, so a missed run is caught up by the next one rather
than needing its own retry logic.

---

## Using it

Open `https://tracker.<your-domain>/`. One day at a time, every device combined
into a single view.

**The day is a vertical time axis.** Stays and trips are the *background* — tinted
bands washing across the full width, each carrying a label that sticks to the top
of the window while you scroll through it — and device activity draws in lanes on
top, so a block always reads against where you were at the time. Lanes come from
whichever event kinds that day actually holds: **Screen** (the frontmost Mac app,
with the websites visited beside it), **Phone**, **Wi-Fi**, **CarPlay**, **Area**.

**Zoom is the main control**, because the data spans five orders of magnitude: a
nine-hour stay and a one-second app switch belong on the same axis. `Day · 1h ·
10m · 1m · 10s` sets the window, `⌘/Ctrl + wheel` zooms about the pointer, `+`/`-`
step, and **double-clicking any block zooms to fit it**. Plain scrolling scrolls.
A preset jumps to whatever is selected, or to the activity nearest wherever you're
already looking, rather than the raw center of the current view — so `10m` never
strands you in an empty hour. The ruler's own tick resolution follows the zoom,
from hourly marks down to every 10 seconds at the deepest level.

At a wide zoom, anything too small to label collapses into a **cluster** — one
hatched block reading `Google Chrome ×34` — which dissolves back into its
individual events the moment the zoom makes them readable. Nothing is hidden and
nothing is faked: a block's size is always its true duration. Event timestamps are
whole seconds, so `10s` is as fine as the record goes.

Colour is per *subject*, not per lane: Chrome is Chrome-blue on the Mac and on the
phone, YouTube red, iTerm2 green. macOS internals that take focus without you
choosing them (`loginwindow`, `coreautha`) sit muted so real apps stand out. Every
block also carries its own text, so colour never has to be read alone.

- **Arrow keys** or the date field move between days.
- **Click** anything — a stay, a trip, one event, a cluster — to fill the
  inspector: times, duration, fixes, radius, a stay's confidence broken into its
  five components, and its note. **Rename** a place there, or by double-clicking
  its map marker. Naming is permanent and applies to every other stay at that
  spot, past and future — you name somewhere once.
- **Click a lane header** to collapse it to a density strip and click again to
  restore it; the choice persists.
- The **rail down the left edge** mirrors the track's own pan and zoom exactly —
  place and trip identity (colour, and a vertical name/duration label where
  there's room) for whatever slice of the day the track currently shows.
- **Hovering the track** draws a time cursor and walks a marker along the route on
  the map — scrub down the day to see where you were.
- An event that spans a place boundary — still on a call as you start driving,
  say — draws as one continuous block crossing it, keeping its own true start
  and end times.
- **Raw fixes** toggles every individual fix with its accuracy circle, flagged
  ones in red. This is the feature, not a debug view: it is how you judge whether
  a stay is real or an artefact of bad reception, and it is exactly what a tracker
  that discards raw data cannot offer.

### Where the day went

`/breakdown` answers a different question from the timeline: not what happened
and when, but how the 24 hours divide up. It draws one day as a two-ring donut —
the inner ring is **where** you were, the outer ring subdivides each place by
**what** you were doing there — with the list beside it as the legend and the
exact numbers. Hovering either one highlights the other. The day bar works the
same as on the timeline, and the day you are looking at follows you between the
two pages.

Both rings total the whole day, which takes some deciding, because the streams
overlap: sleep runs through whichever stay was in progress, a website sits inside
its browser's focus block, and two phone apps can be open at once. Summed
naively a day comes to forty-odd hours. So every instant is given exactly one
place and exactly one activity, and the parts add up to the day exactly.

Activities are a fixed set of six. `Sleep`, `Reddit`, `YouTube` and `Chrome` are
the named ones; anything else on a device is `Other`, and time with no signal at
all is `Untracked`. Reddit and YouTube are one slice each regardless of source,
so the phone's Reddit app and `reddit.com` on the MacBook count together. The
phone reports which app is open but never which site, so browsing Reddit in the
phone's browser is inside `Chrome`.

Where two streams cover the same instant, the more specific one wins: sleep
first, then the MacBook's current site, then its frontmost app, then the phone.
The MacBook outranks the phone because a frontmost-app signal is a real
observation of what is on screen, while a phone app range can sit open long
after you have put the phone down. Within one stream, the range that started
most recently wins.

Time you were somewhere unrecorded is drawn, not hidden — `No location` and
`Untracked` are ordinary wedges in muted grey. The chart doubles as a coverage
report, and on a day the phone spent offline that is most of what it has to say.

**Draw areas before you bother with geocoding.** Ten boxes — home, work, gym,
parents — name about 80 % of your stay-time with no external calls and no
ambiguity:

```bash
curl -X POST https://tracker.<your-domain>/api/v1/areas \
  -H 'Content-Type: application/json' \
  -d '{"name":"Home","min_lat":51.5064,"min_lon":-0.1288,"max_lat":51.5084,"max_lon":-0.1268}'
```

Reverse geocoding is **off by default** (`GEOCODING_ENABLED`). It sends your
coordinates to a public service; turn it on deliberately or not at all.

**The map tiles are the one thing that leaves the box while you use it.** Leaflet
is vendored and the app makes no other third-party request, but the tiles behind
the map come from `tile.openstreetmap.org`, so opening a day asks OpenStreetMap
for imagery covering wherever you were — your home and workplace, at the zoom
you are looking at them. `Referrer-Policy: no-referrer` keeps your hostname out
of those requests, and the tile coordinates themselves are the price of a map
you did not have to host. Serving tiles from this machine is what closes it, and
that means running a tile server.

---

## API

Everything is provider-neutral: no phone app's name appears in a path, so
swapping recorders never means reconfiguring the phone. Which app sent a payload
is detected from its shape.

| | |
|---|---|
| `POST /api/v1/locations` | ingest — **the only write-open path**, token required |
| `GET /api/v1/points` | raw fixes, keyset-paginated by `since_id` |
| `GET /api/v1/days/{date}` | a day assembled: stays and trips, events paired into ranges, a summary, and the breakdown behind `/breakdown` |
| `GET /api/v1/stays` · `PATCH /api/v1/stays/{id}` | query, name, annotate |
| `GET /api/v1/trips` | |
| `GET /api/v1/events` | raw event pings, unpaired — see `/api/v1/days` for the paired view |
| `GET POST PATCH DELETE /api/v1/places` | |
| `GET POST DELETE /api/v1/areas` | |
| `GET /api/v1/devices` · `GET /api/v1/stats` | |
| `POST /api/v1/reprocess` | rebuild the derived layer |
| `GET /healthz` | |

Ingest reads the token from a header: `Authorization: Bearer <token>`, or an
HTTP Basic password, which is what OwnTracks sends — OwnTracks iOS can set Basic
auth but not arbitrary headers. Keeping it out of the URL keeps it out of
uvicorn's access log, which records the query string.

Pagination is keyset, not `OFFSET`: an offset scan re-reads every row it skips and
falls apart once a range runs to hundreds of pages.

---

## Tuning

Every threshold is in `.env`. Change one, rebuild, compare. Raw fixes are never
touched by a rebuild and neither are your names and notes:

```bash
sudo systemctl restart retrace
curl -X POST https://tracker.<your-domain>/api/v1/reprocess
```

The ones that matter:

| Setting | Default | What it does |
|---|---|---|
| `STAY_RADIUS_M` | 70 | How far you can wander and still count as "here" |
| `STAY_DRIFT_CAP` | 1.5 | Multiple of the radius you may drift from a stay's *first* fix. Stops a slow walk collapsing into one fake visit at the midpoint |
| `STAY_MIN_SECONDS` | 300 | Minimum dwell to count as a stay |
| `GAP_MAX_SECONDS` | 3600 | Silence beyond which displacement decides whether a stay continues |
| `GAP_RESUME_DISTANCE_M` | 100 | After a gap, this close means you never left |
| `GAP_RESUME_MAX_SECONDS` | 43200 | …but only up to this much silence. An overnight gap at home is one stay; three days of it is not |
| `MAX_DETOUR_SPEED_MPS` | 83 | Out-and-back speed above which a run of fixes is a stale-fix artefact |
| `BREAKDOWN_TRIP_MIN_FIXES_PER_HOUR` | 4 | Fix density below which `/breakdown` reads a trip as a gap in the record rather than a journey |

**Accuracy is a weight, not a filter.** Fixes are never dropped for a large
accuracy radius — that radius is a confidence estimate, not proof the position is
wrong, and discarding those fixes replaces real route geometry with straight
lines. Only genuinely absurd values (>10 km) and Null Island are flagged, and
flagged fixes are *kept*, just excluded from derived output.

To see the effect of a change before your own data is dense enough to judge:

```bash
INGEST_TOKEN=$(grep ^INGEST_TOKEN .env | cut -d= -f2) \
  .venv/bin/python scripts/synth_day.py --days 7
```

---

## Backups

`retrace-backup.timer` runs nightly at 04:17 UTC (21:17 local), keeping 7 daily and 4 weekly
gzipped snapshots in `data/backups/`. `Persistent=yes`, so a machine that was off
at 04:17 backs up when it comes back.

It uses SQLite's online backup API rather than copying the file: the database is
in WAL mode and being written to, so a byte-for-byte copy can capture a torn page
and a stale `-wal` — an archive that only turns out to be unreadable on the day
you need it. Each snapshot is opened and integrity-checked before it is allowed
to displace an older one.

```bash
.venv/bin/python scripts/backup.py     # run it now
systemctl list-timers retrace-backup   # when it next fires
```

Restore:

```bash
sudo systemctl stop retrace
gunzip -c data/backups/daily/tracker-20260804.db.gz > data/tracker.db
sudo systemctl start retrace
```

The derived layer needs no backup at all — `POST /api/v1/reprocess` rebuilds all
of it from the raw fixes. Only `points`, `places`, `areas` and `stay_notes` hold
anything irreplaceable.

---

`app/db.py` holds the schema as a single `SCHEMA` string, run once against a
database with no tables in it. Changing a database that already holds data is a
manual step during a restart: edit `SCHEMA`, then apply the same change by hand.

```bash
sudo systemctl stop retrace
.venv/bin/python scripts/backup.py
sqlite3 data/tracker.db <<'SQL'
ALTER TABLE events ADD COLUMN device TEXT;
DROP INDEX events_dedup;
CREATE UNIQUE INDEX events_dedup ON events(source, ts, kind, IFNULL(subject, ''), IFNULL(device, ''));
CREATE INDEX events_device_ts ON events(device, ts);
SQL
sudo systemctl start retrace
```

That's the exact change `events.device` needed when it was added — a live
example as much as a template. `ALTER TABLE ... ADD COLUMN` is safe against a
running database: it just adds the column, and existing rows get `NULL`.

A type change, a new CHECK or a column reorder needs the create-copy-drop-rename
dance instead, in one transaction:

```sql
BEGIN;
CREATE TABLE areas_new (...);
INSERT INTO areas_new SELECT id, name, ... FROM areas;
DROP TABLE areas;
ALTER TABLE areas_new RENAME TO areas;
COMMIT;
```

The service is the only writer, so with it stopped the database is yours alone.
Take the backup first: it is the rollback.

---

## How it decides things

```
points          raw fixes, immutable, never deleted, never downsampled
  ↓             quality flags — computed, non-destructive
stays + trips   derived; delete-and-rebuild over a window, idempotent
  ↓
places          user edits live HERE, and a rebuild never touches them
```

Three rules hold the whole design together:

**Raw fixes are immutable.** Bad ones are flagged, never removed. Every derived
query adds `AND anomaly IS NOT 1`. If a flag turns out to be wrong, the data is
still there.

**Points are never stamped with the stay they belong to.** A stay is recomputed
from the points in its window every time. Claiming points means a stay can never
grow or be re-evaluated when fixes arrive late — which is normal on iOS, where a
phone that was out of signal uploads an hour of history at once.

**User edits are a separate layer.** Names, notes and places live in tables the
rebuild does not write to, with `name_locked_at` recording that you chose a name
deliberately. A nightly job silently clobbering a name you set is the failure that
makes people abandon a tracker.

Stay detection is a single-pass sweep with two departures from the usual approach:
a **drift cap** on distance from the stay's first fix, so a slow walk cannot drag
the running centroid along behind it; and **gap handling by displacement** rather
than by blind splitting, so tracking that stops when you settle at the gym and
resumes as you leave still produces one stay rather than none.

Every stay carries a **confidence score** (0–100) with its breakdown stored
alongside it — dwell, tightness, place match, density, accuracy — so you can sort
by it, hide the weak ones, and debug the algorithm against real data instead of
guessing.

Timezone is resolved **per stay from its own coordinates**, not from one account
setting, so a travel day puts each stay on the correct local date.

`events` sits outside this pipeline — a flat, independently-sourced log
(OwnTracks geofence transitions, Shortcuts device signals) with no rebuild
step of its own. The day view pairs its start/end pings into ranges at read
time, the same way `timeline.py` already assembles stays and trips into a
day: a presentation step, not a stored derivation.

---

## Tests

```bash
.venv/bin/python -m pytest
```

`tests/conftest.py` has a `Track` builder that produces deterministic point
streams — "sat still for six hours, drove 40 km, phone went quiet for two hours" —
so tuning is measurable rather than a matter of opinion. `test_segment.py` is the
one that matters: each case in it is a real failure mode (a stay across midnight,
a gap while stationary versus a gap while travelling, a slow walk, a loop returning
to its start, a teleport outlier mid-drive, two devices interleaved).

---

## Browser inspection

`scripts/inspect_page.py` drives headless Chromium (the `playwright` package, listed
in `requirements-dev.txt`) to read a rendered page back as structured text — element
positions, dataset attributes, trimmed text, and the resolved `--accent` colour each
block carries — plus any console output and JS errors. It never takes a screenshot;
an image costs far more tokens than the same information as text, and everything the
day view renders (block position, colour, label) is readable straight off the DOM.

Point it at a throwaway instance so iterating doesn't disturb the live service or
depend on whatever today's real data happens to contain:

```bash
DB_PATH=data/dev.db PORT=8421 INGEST_TOKEN=dev-token \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8421 &

INGEST_TOKEN=dev-token .venv/bin/python scripts/synth_day.py --url http://127.0.0.1:8421

.venv/bin/python scripts/inspect_page.py --url http://127.0.0.1:8421/ \
  --fill "#date=2026-06-03" --select ".block" --select ".place-band"
```

`--fill` and `--click` (both repeatable) drive the page before inspecting — set the
date input, click a zoom preset, click a block — and `--select` chooses which
elements to dump (default `.block`). Nothing stops it from pointing at the real
`retrace.service` on `127.0.0.1:8420` instead, when inspecting real data is actually
what's useful.

One-time setup: `.venv/bin/pip install -r requirements-dev.txt`, then
`.venv/bin/playwright install chromium` — this downloads a browser binary
independent of pip, into `~/.cache/ms-playwright`. Running it also needs a handful
of system shared libraries (`sudo playwright install-deps`, or the equivalent
`apt-get install` for `libatk1.0-0t64 libgbm1 ...`), a one-time host-level step.

---

## Troubleshooting

**No data arriving.** `journalctl -fu retrace` while you move around.

- **Nothing at all in the log** means the request never reached the app —
  Cloudflare rejected it first. Check the Service Auth application is on the
  exact path `/api/v1/locations`, and that the two `CF-Access-Client-*` headers
  in OwnTracks' `httpHeaders` field match the service token exactly (that field
  is single-line — see the setup table above; a real line break where a literal
  `\n` belongs will break it silently).
- **A `400` in the log**, with `Invalid host header` in the body, means
  `PUBLIC_HOSTNAME` in `.env` doesn't match the hostname the tunnel routes. It
  wants the bare name — `tracker.example.com` — and emptying it disables the
  check while you work out which is which.
- **A `401` in the log** means the request reached the app but the password in
  OwnTracks doesn't match `INGEST_TOKEN`.
- **A `200` in the log but no new row in the database** means the payload
  parsed but carried no location — almost always OwnTracks' Passphrase field
  being set, which wraps every message as encrypted and the server correctly
  discards it. Clear that field.

**Gaps in the day.** Usually iOS. Check Low Power Mode, that Location is *Always*
and *Precise*, that Background App Refresh is on, and that the app was not
force-quit. Turn on raw fixes and look at the battery values around the gap.

**A stay in the wrong place, or a walk recorded as a stay.** Turn on raw fixes and
look at the accuracy circles first — often the fixes really are that scattered.
Then try lowering `STAY_RADIUS_M` or `STAY_DRIFT_CAP` and reprocessing.

**Two visits merged into one.** The gap between them was under
`GAP_RESUME_MAX_SECONDS` and you came back to within `GAP_RESUME_DISTANCE_M`.
Lower either.

---

## Not built yet

`trips.mode` exists and is unused. Activity classification, a Google Takeout
import, and multi-user support are all out of scope for now.
