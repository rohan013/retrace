#!/usr/bin/env python3
"""retrace macOS activity agent.

Posts session/focus/site events to the retrace server using the same wire
contract the iPhone's Shortcuts automation uses (see README.md for the full
build spec and app/timeline.py in the parent repo for the server-side
pairing rules). Runs as a LaunchAgent; see install.sh.

All tracking/networking logic lives on ActivityTracker, a plain Python
class with no Cocoa dependency. NotificationBridge is a thin NSObject
adapter that exists only to receive real Cocoa notifications and forward
them -- pyobjc auto-bridges every method on an NSObject subclass to an
Objective-C selector by inferring its signature from the method's name, so
keeping business-logic methods off that class avoids fighting the bridge.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit

import objc
import requests
from AppKit import NSWorkspace
from Foundation import NSDistributedNotificationCenter, NSObject
from PyObjCTools import AppHelper
from Quartz import CGSessionCopyCurrentDictionary


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_env(os.path.join(BASE_DIR, ".env"))

SERVER_URL = os.environ["SERVER_URL"]
INGEST_TOKEN = os.environ["INGEST_TOKEN"]
CF_ACCESS_CLIENT_ID = os.environ["CF_ACCESS_CLIENT_ID"]
CF_ACCESS_CLIENT_SECRET = os.environ["CF_ACCESS_CLIENT_SECRET"]
DEVICE = os.environ.get("DEVICE", "macbook")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "4"))
QUEUE_PATH = os.path.join(BASE_DIR, os.environ.get("QUEUE_PATH", "queue.jsonl"))

QUEUE_FLUSH_INTERVAL_SECONDS = 45
POLL_JOIN_TIMEOUT_SECONDS = 3  # longer than OSASCRIPT_TIMEOUT_SECONDS below
OSASCRIPT_TIMEOUT_SECONDS = 2
POST_TIMEOUT_SECONDS = 5

AUTH_HEADERS = {
    "Authorization": f"Bearer {INGEST_TOKEN}",
    "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
    "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
}

# bundle ID -> AppleScript application name. Only browsers whose `mode`
# property has been verified by hand belong here -- see README.md's "Site
# polling" section. Chrome verified 2026-08-05 (both "normal" and
# "incognito" confirmed). Brave/Edge/Arc aren't installed on this machine
# and haven't been checked; add an entry only after confirming with
# `osascript -e 'tell application "<name>" to get mode of front window'`
# against both a normal and an incognito window.
TRACKED_BROWSERS: dict[str, str] = {
    "com.google.Chrome": "Google Chrome",
    # "com.brave.Browser": "Brave Browser",
    # "com.microsoft.edgemac": "Microsoft Edge",
    # "company.thebrowser.Browser": "Arc",
}

_TAB_SCRIPT = """
tell application "{app}"
    if (count of windows) = 0 then return "||"
    set theURL to URL of active tab of front window
    set theMode to mode of front window
end tell
return theURL & "||" & theMode
"""


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def get_active_tab(bundle_id: str) -> tuple[str | None, str | None]:
    app_name = TRACKED_BROWSERS[bundle_id]
    result = subprocess.run(
        ["osascript", "-e", _TAB_SCRIPT.format(app=app_name)],
        capture_output=True,
        text=True,
        timeout=OSASCRIPT_TIMEOUT_SECONDS,
        check=True,
    )
    url, _, mode = result.stdout.strip().partition("||")
    return (url, mode) if url else (None, None)


class ActivityTracker:
    """All session/focus/site tracking, networking, and queueing logic.

    Plain Python -- no Cocoa dependency -- so pyobjc's NSObject method
    bridging never applies to it.
    """

    def __init__(self) -> None:
        self.session_open = False
        self.current_app_name: str | None = None
        self.current_app_bundle: str | None = None
        self.current_site: str | None = None
        self.state_lock = threading.Lock()
        self.queue_lock = threading.Lock()
        self.poll_thread: threading.Thread | None = None
        self.poll_stop: threading.Event | None = None

    # ---- session ----

    def maybe_open_session(self) -> None:
        if not self.session_open:
            self.emit("session", value="unlock")
            self.session_open = True

    def maybe_close_session(self) -> None:
        if self.session_open:
            self.emit("session", value="lock")
            self.session_open = False

    # ---- focus ----

    def on_activate(self, new_name: str, new_bundle: str) -> None:
        if new_name == self.current_app_name:
            return  # spurious re-activation of the same app -- not a real switch

        old_bundle = self.current_app_bundle

        # Closing site tracking here, before app-level state flips, keeps
        # this one code path the single source of truth for "focus left a
        # tracked browser" -- stop_poll_loop() joins first so an in-flight
        # poll tick can't emit a stale site-end after this one.
        if old_bundle in TRACKED_BROWSERS:
            self.stop_poll_loop()
            with self.state_lock:
                if self.current_site is not None:
                    self.emit("site", value="end", subject=self.current_site)
                    self.current_site = None

        self.current_app_name, self.current_app_bundle = new_name, new_bundle
        self.emit("focus", value="start", subject=new_name)

        if new_bundle in TRACKED_BROWSERS:
            self.start_poll_loop()

    # ---- site polling ----

    def start_poll_loop(self) -> None:
        self.poll_stop = threading.Event()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def stop_poll_loop(self) -> None:
        if self.poll_thread is None:
            return
        self.poll_stop.set()
        self.poll_thread.join(timeout=POLL_JOIN_TIMEOUT_SECONDS)
        self.poll_thread = None
        self.poll_stop = None

    def _poll_loop(self) -> None:
        # start_poll_loop() is only called right after current_app_bundle is
        # set to a tracked browser, so a plain unlocked read here is safe --
        # see the "Threading note" in README.md.
        bundle_id = self.current_app_bundle
        poll_stop = self.poll_stop
        while True:
            try:
                url, mode = get_active_tab(bundle_id)
                site = "incognito" if mode == "incognito" else (domain_of(url) or "no tab")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
                site = "no tab"  # e.g. front window is a downloads popup, not a tab

            with self.state_lock:
                if site != self.current_site:
                    self.emit("site", value="start", subject=site)
                    self.current_site = site

            if poll_stop.wait(POLL_INTERVAL_SECONDS):
                return

    # ---- startup sync ----

    def sync_startup_state(self) -> None:
        info = CGSessionCopyCurrentDictionary() or {}
        locked_at_startup = bool(info.get("CGSSessionScreenIsLocked", False))
        if not locked_at_startup:
            self.maybe_open_session()

        frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
        if frontmost is not None:
            self.on_activate(frontmost.localizedName(), frontmost.bundleIdentifier())

        log(
            f"startup sync: locked={locked_at_startup} "
            f"app={self.current_app_name!r} bundle={self.current_app_bundle!r}"
        )

    # ---- sending events, and the offline retry queue ----

    def emit(self, kind: str, value: str, subject: str | None = None) -> None:
        payload = {
            "kind": kind,
            "value": value,
            "subject": subject,
            "device": DEVICE,
            "ts": int(time.time()),
        }
        log(f"emit {payload}")
        if not self._post(payload):
            self._queue_append(payload)

    def _post(self, payload: dict) -> bool:
        try:
            r = requests.post(
                SERVER_URL,
                params={"format": "shortcuts"},
                json=payload,
                headers=AUTH_HEADERS,
                timeout=POST_TIMEOUT_SECONDS,
            )
            return r.status_code == 200
        except requests.RequestException as exc:
            log(f"post failed: {exc}")
            return False

    def _queue_append(self, payload: dict) -> None:
        with self.queue_lock:
            with open(QUEUE_PATH, "a") as f:
                f.write(json.dumps(payload) + "\n")
        log(f"queued for retry: {payload}")

    def flush_queue(self) -> None:
        with self.queue_lock:
            if not os.path.exists(QUEUE_PATH):
                return
            with open(QUEUE_PATH) as f:
                lines = [line for line in f if line.strip()]

            flushed = 0
            for line in lines:
                if not self._post(json.loads(line)):
                    break
                flushed += 1

            if flushed == 0:
                return

            remaining = lines[flushed:]
            with open(QUEUE_PATH, "w") as f:
                f.writelines(remaining)
            log(f"flushed {flushed} queued event(s), {len(remaining)} remaining")


class NotificationBridge(NSObject):
    """Thin Cocoa adapter -- the only pyobjc-bridged class. Every method
    here really is meant to be an Objective-C selector, so there's nothing
    for pyobjc's auto-bridging to trip over."""

    def initWithTracker_(self, tracker: ActivityTracker) -> "NotificationBridge | None":
        self = objc.super(NotificationBridge, self).init()
        if self is None:
            return None
        self.tracker = tracker
        return self

    def onActivate_(self, notification) -> None:
        app = notification.userInfo().get("NSWorkspaceApplicationKey")
        if app is not None:
            self.tracker.on_activate(app.localizedName(), app.bundleIdentifier())

    def onSleep_(self, notification) -> None:
        self.tracker.maybe_close_session()

    def onWake_(self, notification) -> None:
        self.tracker.maybe_open_session()

    def onScreenLocked_(self, notification) -> None:
        self.tracker.maybe_close_session()

    def onScreenUnlocked_(self, notification) -> None:
        self.tracker.maybe_open_session()


def _flush_timer_loop(tracker: ActivityTracker, stop_event: threading.Event) -> None:
    while True:
        tracker.flush_queue()
        if stop_event.wait(QUEUE_FLUSH_INTERVAL_SECONDS):
            return


def main() -> None:
    tracker = ActivityTracker()
    bridge = NotificationBridge.alloc().initWithTracker_(tracker)

    ws_nc = NSWorkspace.sharedWorkspace().notificationCenter()
    ws_nc.addObserver_selector_name_object_(
        bridge, "onActivate:", "NSWorkspaceDidActivateApplicationNotification", None
    )
    ws_nc.addObserver_selector_name_object_(
        bridge, "onSleep:", "NSWorkspaceWillSleepNotification", None
    )
    ws_nc.addObserver_selector_name_object_(
        bridge, "onWake:", "NSWorkspaceDidWakeNotification", None
    )

    dnc = NSDistributedNotificationCenter.defaultCenter()
    dnc.addObserver_selector_name_object_(
        bridge, "onScreenLocked:", "com.apple.screenIsLocked", None
    )
    dnc.addObserver_selector_name_object_(
        bridge, "onScreenUnlocked:", "com.apple.screenIsUnlocked", None
    )

    tracker.sync_startup_state()

    flush_stop = threading.Event()
    threading.Thread(
        target=_flush_timer_loop, args=(tracker, flush_stop), daemon=True
    ).start()

    log(f"agent started, device={DEVICE!r}, tracked_browsers={list(TRACKED_BROWSERS.values())}")
    AppHelper.runConsoleEventLoop(installInterrupt=True)


if __name__ == "__main__":
    main()
