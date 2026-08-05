# tracker

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
  db.py         schema and migrations
  ingest.py     accepting fixes
  quality.py    flagging bad ones (never deleting them)
  segment.py    turning fixes into stays and trips
  places.py     turning stays into places
  timeline.py   assembling a day
  providers/    one file per phone app; add a file, not a branch
static/       the web UI
scripts/      backup, synthetic data
deploy/       systemd units
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

Checks `.env` and the venv are actually set up, then installs and starts
`tracker.service` and `tracker-backup.timer`. Safe to re-run any time the unit
files change. `deploy/uninstall.sh` stops and removes them again, leaving the
repo, `.env` and `data/` untouched.

It listens on `127.0.0.1:8420` only. Nothing reaches it except through the tunnel.

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

### 2. Protect the UI

**Zero Trust → Access controls → Applications → Create new application →
Self-hosted and private → Add public hostname:**

| | |
|---|---|
| Domain | `tracker.<your-domain>` (no path) |
| Policy | **Allow**, Include → Emails → `rohan9513@gmail.com` |

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

`journalctl -fu tracker` should show nothing for the first request (Cloudflare
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

## Using it

Open `https://tracker.<your-domain>/`.

- **Arrow keys** or the date field move between days.
- **Click** a timeline entry to focus it on the map; **double-click** to name the
  place. Naming is permanent and applies to every other stay at that spot, past
  and future — you name somewhere once.
- **Raw fixes** toggles every individual fix with its accuracy circle, flagged
  ones in red. This is the feature, not a debug view: it is how you judge whether
  a stay is real or an artefact of bad reception, and it is exactly what a tracker
  that discards raw data cannot offer.

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

---

## API

Everything is provider-neutral: no phone app's name appears in a path, so
swapping recorders never means reconfiguring the phone. Which app sent a payload
is detected from its shape.

| | |
|---|---|
| `POST /api/v1/locations` | ingest — **the only write-open path**, token required |
| `GET /api/v1/points` | raw fixes, keyset-paginated by `since_id` |
| `GET /api/v1/days/{date}` | a day assembled: stays and trips interleaved, plus a summary |
| `GET /api/v1/stays` · `PATCH /api/v1/stays/{id}` | query, name, annotate |
| `GET /api/v1/trips` | |
| `GET POST PATCH DELETE /api/v1/places` | |
| `GET POST DELETE /api/v1/areas` | |
| `GET /api/v1/devices` · `GET /api/v1/stats` | |
| `POST /api/v1/reprocess` | rebuild the derived layer |
| `GET /healthz` | |

Ingest accepts the token as `Authorization: Bearer <token>`, as an HTTP Basic
password (what OwnTracks sends), or as `?token=` — OwnTracks iOS can set Basic
auth but not arbitrary headers.

Pagination is keyset, not `OFFSET`: an offset scan re-reads every row it skips and
falls apart once a range runs to hundreds of pages.

---

## Tuning

Every threshold is in `.env`. Change one, rebuild, compare. Raw fixes are never
touched by a rebuild and neither are your names and notes:

```bash
sudo systemctl restart tracker
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

`tracker-backup.timer` runs nightly at 04:17 UTC (21:17 local), keeping 7 daily and 4 weekly
gzipped snapshots in `data/backups/`. `Persistent=yes`, so a machine that was off
at 04:17 backs up when it comes back.

It uses SQLite's online backup API rather than copying the file: the database is
in WAL mode and being written to, so a byte-for-byte copy can capture a torn page
and a stale `-wal` — an archive that only turns out to be unreadable on the day
you need it. Each snapshot is opened and integrity-checked before it is allowed
to displace an older one.

```bash
.venv/bin/python scripts/backup.py     # run it now
systemctl list-timers tracker-backup   # when it next fires
```

Restore:

```bash
sudo systemctl stop tracker
gunzip -c data/backups/daily/tracker-20260804.db.gz > data/tracker.db
sudo systemctl start tracker
```

The derived layer needs no backup at all — `POST /api/v1/reprocess` rebuilds all
of it from the raw fixes. Only `points`, `places`, `areas` and `stay_notes` hold
anything irreplaceable.

---

## How it decides things

```
points          raw fixes, immutable, never deleted, never downsampled
  ↓             quality flags — computed, non-destructive
stays + trips   derived; delete-and-rebuild over a window, idempotent
  ↓
places          user edits live HERE, and a rebuild never touches them
  ↓
events          empty for now — the extension point
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

## Troubleshooting

**No data arriving.** `journalctl -fu tracker` while you move around.

- **Nothing at all in the log** means the request never reached the app —
  Cloudflare rejected it first. Check the Service Auth application is on the
  exact path `/api/v1/locations`, and that the two `CF-Access-Client-*` headers
  in OwnTracks' `httpHeaders` field match the service token exactly (that field
  is single-line — see the setup table above; a real line break where a literal
  `\n` belongs will break it silently).
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

The `events` table and `trips.mode` exist and are empty — they are where passive
sources (iOS Shortcuts, email receipts, calendar) attach later, without a schema
change. There is no activity classification, no Takeout import, no multi-user.
