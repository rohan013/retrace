# retrace macOS agent — implementation spec

This is a build spec for `agent.py`, the one file in this directory that
doesn't exist yet. Everything else here (`requirements.txt`, `.env.example`,
the LaunchAgent plist) is already in place. If you're a fresh Claude Code
session picking this up with no other context: read this whole file before
writing any code, and read the server's own `README.md` (one level up) for
the full project background — the "iPhone — Shortcuts" section in particular
is the existing pattern this agent mirrors.

## What this is

`retrace` is a self-hosted location/activity tracker. It already ingests
device-activity signals from an iPhone via personal Shortcuts automations
(app opened/closed, wifi connected/disconnected, CarPlay connected/
disconnected) — each signal a start/end pair, paired into a range at read
time and shown in a day-timeline view. The server side of this repo has
already been extended to accept three new signal kinds for a MacBook, using
the exact same wire contract the phone uses. **Nothing on the server needs
to change** — this spec is entirely about writing the Mac-side daemon that
sends these pings.

Three signals:

1. **`session`** — screen unlock/wake vs. lock/sleep. Answers "when did I
   start/stop using this machine".
2. **`focus`** — which app is frontmost, as it changes. Answers "what was I
   using, and when".
3. **`site`** — while a tracked browser is frontmost, the domain of its
   active tab. Answers "what site was I on", excluding private/incognito
   windows.

## Wire contract (already live on the server — verify, don't guess)

POST to the same ingest path the phone uses, with `?format=shortcuts` so
detection can't fail silently:

```
POST {SERVER_URL}?format=shortcuts
Authorization: Bearer {INGEST_TOKEN}
CF-Access-Client-Id: {CF_ACCESS_CLIENT_ID}
CF-Access-Client-Secret: {CF_ACCESS_CLIENT_SECRET}
Content-Type: application/json

{"kind": "focus", "subject": "Terminal", "value": "start", "device": "macbook", "ts": 1780000000}
```

Field contract (from the server's `app/providers/shortcuts.py`):
- `kind` — required, one of `session` / `focus` / `site` for this agent.
- `subject` — free text; `None`/omitted for `session`, the app's name for
  `focus`, a bare domain for `site`.
- `value` — the literal string that pairs into a range server-side (see
  table below). Anything else is stored but won't pair.
- `device` — always `"macbook"` (or whatever `DEVICE` is set to in `.env`).
- `ts` — **always send this explicitly**, as a unix integer timestamp taken
  at the moment the handler fires. Don't rely on the server's receive-time
  default. Session/focus pairs depend on the *relative* order of two
  close-together POSTs (e.g. an `end` for the app losing focus and a
  `start` for the one gaining it, fired within the same handler call) — a
  network delay hitting only one of them could scramble that order if the
  server had to fall back to its own clock.

| Signal | `kind` | `value` pair | `subject` |
|---|---|---|---|
| Session | `session` | `unlock` / `lock` | `None` |
| Focus | `focus` | `start` / `end` | app's localized name, e.g. `"Safari"` |
| Site | `site` | `start` / `end` | bare domain, e.g. `"github.com"` |

Verify this against the live server before writing code that depends on it:
`app/timeline.py`'s `_RANGE_KINDS` dict on the server is the source of
truth for these three entries.

### The pairing asymmetry that shapes this whole design

The server pairs sequential pings per `(device, kind, subject)`, in
timestamp order. A **repeated `start`** while one is already open is
silently dropped as noise — safe to over-send. A **repeated `end`** after a
range is already closed is *not* absorbed — it lands in the day view as a
flagged, unpaired point.

This matters because both `session`'s natural signals — wake vs.
screen-unlock, sleep vs. screen-lock — are pairs of *independent* macOS
notifications that both mean the same semantic thing. If the agent just
forwarded all four raw notifications, you'd get a spurious flagged point on
nearly every lock cycle (whichever of `willSleep`/`screenIsLocked` fires
second has nothing left open to pair with). **The agent must dedup this
itself** — track one `session_open: bool`, and only emit when it actually
flips. Same reasoning applies to `focus`: some apps/dialogs can refire an
activation notification for the app that's *already* frontmost; treat that
as a no-op rather than emitting a zero-length end+start pair, which would
otherwise fragment one real focus period into two ranges.

## Config

Mirror the server's own `.env` reader exactly (`app/config.py` in the
parent repo) rather than adding a second dependency for something this
small — read `KEY=value` lines from `.env` next to `agent.py`, skip
blanks/comments, `os.environ.setdefault`. `.env.example` in this directory
lists every variable: `SERVER_URL`, `INGEST_TOKEN`,
`CF_ACCESS_CLIENT_ID`/`_SECRET`, `DEVICE`, `POLL_INTERVAL_SECONDS`,
`QUEUE_PATH`.

## Notification wiring (Cocoa, via pyobjc)

A plain Python object can't receive Cocoa notification callbacks — the
agent's main class must subclass `Foundation.NSObject`, with handler methods
named using pyobjc's Python-to-selector convention (`onActivate_(self,
notification)` registers as the Objective-C selector `onActivate:`).

```python
from AppKit import NSWorkspace
from Foundation import NSObject, NSDistributedNotificationCenter
from PyObjCTools import AppHelper

class Agent(NSObject):
    def onActivate_(self, notification): ...   # app focus changed
    def onSleep_(self, notification): ...       # NSWorkspaceWillSleepNotification
    def onWake_(self, notification): ...        # NSWorkspaceDidWakeNotification
    def onScreenLocked_(self, notification): ...
    def onScreenUnlocked_(self, notification): ...

agent = Agent.alloc().init()

ws_nc = NSWorkspace.sharedWorkspace().notificationCenter()
ws_nc.addObserver_selector_name_object_(
    agent, "onActivate:", "NSWorkspaceDidActivateApplicationNotification", None)
ws_nc.addObserver_selector_name_object_(
    agent, "onSleep:", "NSWorkspaceWillSleepNotification", None)
ws_nc.addObserver_selector_name_object_(
    agent, "onWake:", "NSWorkspaceDidWakeNotification", None)

dnc = NSDistributedNotificationCenter.defaultCenter()
dnc.addObserver_selector_name_object_(agent, "onScreenLocked:", "com.apple.screenIsLocked", None)
dnc.addObserver_selector_name_object_(agent, "onScreenUnlocked:", "com.apple.screenIsUnlocked", None)

AppHelper.runConsoleEventLoop(installInterrupt=True)  # pumps the run loop; blocks forever
```

The site-tracking poll loop (below) runs on a separate `threading.Thread`
started before `runConsoleEventLoop` blocks the main thread.

## Session state

One boolean, `session_open`. Both wake and screen-unlock call the same
`maybe_open_session()`; both sleep and screen-lock call the same
`maybe_close_session()`:

```python
def maybe_open_session(self):
    if not self.session_open:
        self.emit("session", value="unlock")
        self.session_open = True

def maybe_close_session(self):
    if self.session_open:
        self.emit("session", value="lock")
        self.session_open = False
```

**On startup**, read the actual current lock state so a restart doesn't
miss an already-unlocked session, and initialize `session_open` from it
(don't just default to `False` and wait for the next transition):

```python
from Quartz import CGSessionCopyCurrentDictionary
info = CGSessionCopyCurrentDictionary() or {}
locked_at_startup = bool(info.get("CGSSessionScreenIsLocked", False))
```

## Focus state

Two attributes: `current_app_name`, `current_app_bundle` (bundle ID needed
to check tracked-browser membership for site-polling).

```python
def on_activate(self, new_name, new_bundle):
    if new_name == self.current_app_name:
        return  # spurious re-activation of the same app -- not a real switch

    old_name, old_bundle = self.current_app_name, self.current_app_bundle
    if old_name is not None:
        self.emit("focus", value="end", subject=old_name)

    # Site tracking is keyed off focus, not its own notification: closing it
    # here, before app-level state flips, keeps one code path as the single
    # source of truth for "focus left a tracked browser".
    if old_bundle in TRACKED_BROWSERS and self.current_site is not None:
        self.emit("site", value="end", subject=self.current_site)
        self.current_site = None
        self.stop_poll_loop()

    self.current_app_name, self.current_app_bundle = new_name, new_bundle
    self.emit("focus", value="start", subject=new_name)

    if new_bundle in TRACKED_BROWSERS:
        self.start_poll_loop()  # does one immediate poll -- see below
```

`onActivate_` (the actual Cocoa callback) reads the newly-activated app's
name and bundle ID off the notification's `userInfo`
(`NSWorkspaceApplicationKey`, an `NSRunningApplication`) and calls
`on_activate` with them.

**On startup**, read `NSWorkspace.sharedWorkspace().frontmostApplication()`
and call the *same* `on_activate` handler, with `current_app_name`
initialized to `None` beforehand — reuses one code path for both real
transitions and startup sync, and correctly no-ops into "repeat start,
absorbed as noise" if the daemon is restarting mid-focus-period on an app
that was already open before the crash (see the pairing asymmetry section
above — this is exactly the side of it that *is* safely tolerated).

## Site polling

Only while `current_app_bundle` is one of:

| App | Bundle ID |
|---|---|
| Google Chrome | `com.google.Chrome` |
| Brave Browser | `com.brave.Browser` |
| Microsoft Edge | `com.microsoft.edgemac` |
| Arc | `company.thebrowser.Browser` |

**Verify `mode` before trusting a browser's incognito detection.** Chrome
and Brave are the same Chromium scripting bridge — high confidence. Edge
implements Chrome's automation protocol but the exact property name isn't
independently confirmed here — check it by hand. Arc has historically had a
more minimal AppleScript dictionary with a different window model (built
around Spaces) — check it explicitly:

```bash
osascript -e 'tell application "Arc" to get mode of front window'
```

with a private/incognito window open, and confirm it actually returns
`"incognito"`. **If any of these three doesn't reliably expose `mode`,
exclude it from `TRACKED_BROWSERS` rather than shipping unverified
incognito detection** — same reasoning that already excludes Safari on the
server side (no reliably scriptable private-window flag) and Firefox (no
AppleScript dictionary at all).

AppleScript per poll tick (same body for Chrome/Brave/Edge, substituting
the app name; verify Arc separately since its dictionary may differ):

```applescript
tell application "Google Chrome"
    if (count of windows) = 0 then return "||"
    set theURL to URL of active tab of front window
    set theMode to mode of front window
end tell
return theURL & "||" & theMode
```

Run via `subprocess.run(["osascript", "-e", script], capture_output=True,
text=True, timeout=2)`, split the output on `"||"`. `mode` is a property of
the *window*, not the tab — don't try to read it off `active tab`.

Poll loop, gated by a `threading.Event` so shutdown is instant rather than
waiting out the interval:

```python
def _poll_loop(self):
    while not self.poll_stop.is_set():
        try:
            url, mode = get_active_tab(self.current_app_bundle)
            site = None if mode == "incognito" else domain_of(url)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            site = None  # e.g. front window is a downloads popup, not a tab

        with self.state_lock:
            if site != self.current_site:
                if self.current_site is not None:
                    self.emit("site", value="end", subject=self.current_site)
                if site is not None:
                    self.emit("site", value="start", subject=site)
                self.current_site = site

        self.poll_stop.wait(POLL_INTERVAL_SECONDS)
```

`start_poll_loop()` should do one check *before* the first wait, so
switching into a tracked browser reflects its current tab immediately.

`domain_of(url)`: `urllib.parse.urlsplit(url).hostname`, stripping a
leading `www.`, returning `None` for anything without an `http`/`https`
scheme (`chrome://newtab`, extension pages) so those are treated the same
as "no site" instead of erroring.

**Threading note:** Cocoa callbacks run on the main thread; the poll loop
runs on a background thread; both touch `current_site`/`current_app_bundle`.
Guard the read-compare-write sequence with a `threading.Lock` — individual
attribute accesses are GIL-atomic but the sequence isn't, and a focus-away
landing mid-poll-tick is a real race at human Cmd-Tab timing against a
multi-second poll interval, not a theoretical one.

## Sending events, and the offline retry queue

A laptop is routinely offline while still generating real activity (closed
lid reopened on a flight with wifi off, a captive portal, the server
restarting) — unlike sparse Shortcuts automations, this agent can generate
a lot of signals across a session, so a bare fire-and-forget POST would
lose real usage data to any network blip. Queue on failure, retry on a
timer:

```python
def emit(self, kind, value, subject=None):
    payload = {"kind": kind, "value": value, "subject": subject,
               "device": DEVICE, "ts": int(time.time())}
    if not self._post(payload):
        self._queue_append(payload)

def _post(self, payload) -> bool:
    try:
        r = requests.post(SERVER_URL, params={"format": "shortcuts"}, json=payload,
                           headers=AUTH_HEADERS, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False
```

A background timer (every 30-60s) reads `QUEUE_PATH` (JSONL, one payload
per line), tries to flush entries oldest-first, and stops at the first
failure so order is preserved for the next attempt rather than reshuffled.
Retrying a payload that actually did land before its response was lost is
always safe: `ts` is fixed at emission time, and the server's
`events_dedup` unique index makes a resend an `INSERT OR IGNORE` no-op.

## Installation

```bash
# On the Mac, wherever you want this to live permanently:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in SERVER_URL, INGEST_TOKEN, CF_ACCESS_CLIENT_ID/SECRET

# Sanity-check it directly first, NOT as a LaunchAgent yet:
.venv/bin/python3 agent.py
# -- switch apps, lock/unlock the screen, browse in a tracked browser --
# -- confirm events are arriving via the server's `journalctl -fu retrace`
#    and GET /api/v1/days/{today} --

# Only once that looks right, install it to run automatically:
# 1. Edit com.rohan.retrace-agent.plist -- both paths in ProgramArguments
#    and WorkingDirectory need to point at THIS directory and its venv.
cp com.rohan.retrace-agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rohan.retrace-agent.plist
tail -f /tmp/retrace-agent.log
```

The Cloudflare Service Token (`CF_ACCESS_CLIENT_ID`/`_SECRET`) is a new one
named `tracker-macbook`, created the same way the phone's was — see the
main repo README's "Cloudflare" and new "MacBook" sections.

## Open items to resolve while building this (don't guess past these)

1. **Edge and Arc's `mode` property** — unverified from here (no macOS
   available in the environment this spec was written in). Confirm on the
   real machine per the Site polling section above before enabling either.
2. **Exact `userInfo` key names** for reading the activated app's name/
   bundle ID off an `NSWorkspaceDidActivateApplicationNotification` — the
   key is `NSWorkspaceApplicationKey` giving an `NSRunningApplication`, with
   `.localizedName()` and `.bundleIdentifier()` — confirm this against the
   installed pyobjc version's stubs/docs since exact accessor names can
   drift slightly across pyobjc releases.
3. **`www.` stripping in `site` subjects** is an opinionated normalization
   (`www.reddit.com`/`reddit.com` treated as one site) — keep it, but it's
   a small enough call that it's fine to drop if it turns out to be
   annoying in practice.
