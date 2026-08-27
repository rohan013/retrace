"""Unit tests for macos/agent.py's ActivityTracker.

The module can't be imported as-is outside macOS -- it imports objc, AppKit,
Foundation, PyObjCTools, and Quartz at module level, none of which are
installed in this repo's venv. ActivityTracker itself needs none of them (see
its own docstring: "Plain Python -- no Cocoa dependency"), so it's imported
here by stubbing those five modules, plus requests (also not installed here,
since it's a daemon-only dependency -- see macos/requirements.txt), before
loading the file directly. main() is guarded by `if __name__ == "__main__":`
so importing never starts the real daemon.
"""

import importlib.util
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parent.parent / "macos" / "agent.py"

_STUB_MODULE_NAMES = (
    "objc",
    "AppKit",
    "Foundation",
    "PyObjCTools",
    "PyObjCTools.AppHelper",
    "Quartz",
    "requests",
)


def _load_agent_module(monkeypatch, **extra_env):
    monkeypatch.setenv("SERVER_URL", "http://example.invalid")
    monkeypatch.setenv("INGEST_TOKEN", "test-token")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "test-id")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "test-secret")
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)

    stubs = {name: types.ModuleType(name) for name in _STUB_MODULE_NAMES}
    stubs["AppKit"].NSWorkspace = object
    stubs["Foundation"].NSDistributedNotificationCenter = object
    stubs["Foundation"].NSObject = object
    stubs["PyObjCTools"].AppHelper = stubs["PyObjCTools.AppHelper"]
    stubs["Quartz"].CGSessionCopyCurrentDictionary = lambda: {}
    stubs["requests"].RequestException = Exception
    stubs["requests"].post = lambda *a, **k: None
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("macos_agent_under_test", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent_module(monkeypatch):
    return _load_agent_module(monkeypatch)


@pytest.fixture
def scoped_agent_module(monkeypatch):
    """A deployment restricted to Reddit/YouTube `site` tracking with `focus`
    suppressed entirely -- e.g. a work machine (see TRACKED_SITES/
    EMIT_FOCUS_EVENTS in agent.py)."""
    return _load_agent_module(
        monkeypatch, TRACKED_SITES="reddit.com,youtube.com", EMIT_FOCUS_EVENTS="false"
    )


def _tracked_bundle(agent_module) -> str:
    return next(iter(agent_module.TRACKED_BROWSERS))


def _run_one_poll_tick(tracker) -> None:
    """Drive exactly one _poll_loop iteration synchronously.

    Pre-setting poll_stop means poll_stop.wait(...) returns True immediately
    after the tick body runs, so the loop exits after one pass -- no thread,
    no real osascript call needed, since get_active_tab is mocked by the
    caller first.
    """
    tracker.poll_stop = threading.Event()
    tracker.poll_stop.set()
    tracker._poll_loop()


class _FireOnceEvent:
    """Stand-in for the heartbeat loop's stop_event: False on the first
    wait() (let one heartbeat check run), True on the second (stop there)."""

    def __init__(self) -> None:
        self.calls = 0

    def wait(self, timeout: float) -> bool:
        self.calls += 1
        return self.calls > 1


def test_switching_apps_emits_only_focus_start(agent_module):
    tracker = agent_module.ActivityTracker()
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    tracker.on_activate("Terminal", "com.apple.Terminal")
    tracker.on_activate("Safari", "com.apple.Safari")

    assert emitted == [
        ("focus", "start", "Terminal"),
        ("focus", "start", "Safari"),
    ]


def test_reactivating_the_same_app_emits_nothing(agent_module):
    tracker = agent_module.ActivityTracker()
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    tracker.on_activate("Terminal", "com.apple.Terminal")
    tracker.on_activate("Terminal", "com.apple.Terminal")

    assert emitted == [("focus", "start", "Terminal")]


def test_leaving_a_tracked_browser_still_emits_an_explicit_site_end(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.stop_poll_loop = lambda: None  # no real poll thread was started
    tracker.current_app_name = "Google Chrome"
    tracker.current_app_bundle = _tracked_bundle(agent_module)
    tracker.current_site = "github.com"

    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    tracker.on_activate("Terminal", "com.apple.Terminal")

    assert emitted == [
        ("site", "end", "github.com"),
        ("focus", "start", "Terminal"),
    ]
    assert tracker.current_site is None


def test_incognito_site_gets_a_sentinel_subject(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(agent_module)
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    agent_module.get_active_tab = lambda bundle_id: ("https://private.example/", "incognito")
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "start", "incognito")]
    assert tracker.current_site == "incognito"


def test_unreadable_tab_gets_a_no_tab_sentinel(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(agent_module)
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    def _raise(bundle_id):
        raise subprocess.CalledProcessError(1, "osascript")

    agent_module.get_active_tab = _raise
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "start", "no tab")]


def test_site_switch_never_emits_an_end_mid_session(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(agent_module)
    tracker.current_site = "github.com"
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    agent_module.get_active_tab = lambda bundle_id: ("https://news.ycombinator.com/", "normal")
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "start", "news.ycombinator.com")]


def test_heartbeat_fires_while_session_is_open(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.session_open = True
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    agent_module._heartbeat_loop(tracker, _FireOnceEvent())

    assert emitted == [("session", "heartbeat", None)]


def test_heartbeat_emits_nothing_while_session_is_closed(agent_module):
    tracker = agent_module.ActivityTracker()
    tracker.session_open = False
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    agent_module._heartbeat_loop(tracker, _FireOnceEvent())

    assert emitted == []


# -- TRACKED_SITES / EMIT_FOCUS_EVENTS scoping (e.g. a work machine) ---------


def test_an_untracked_site_is_never_emitted(scoped_agent_module):
    tracker = scoped_agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(scoped_agent_module)
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    scoped_agent_module.get_active_tab = lambda bundle_id: ("https://news.ycombinator.com/", "normal")
    _run_one_poll_tick(tracker)

    assert emitted == []
    assert tracker.current_site is None


def test_a_tracked_site_is_emitted_and_subdomains_match(scoped_agent_module):
    tracker = scoped_agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(scoped_agent_module)
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    scoped_agent_module.get_active_tab = lambda bundle_id: ("https://old.reddit.com/r/test", "normal")
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "start", "old.reddit.com")]


def test_leaving_a_tracked_site_for_an_untracked_one_emits_an_explicit_end(scoped_agent_module):
    tracker = scoped_agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(scoped_agent_module)
    tracker.current_site = "reddit.com"
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    scoped_agent_module.get_active_tab = lambda bundle_id: ("https://news.ycombinator.com/", "normal")
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "end", "reddit.com")]
    assert tracker.current_site is None


def test_incognito_is_treated_as_untracked_when_scoped(scoped_agent_module):
    tracker = scoped_agent_module.ActivityTracker()
    tracker.current_app_bundle = _tracked_bundle(scoped_agent_module)
    tracker.current_site = "youtube.com"
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    scoped_agent_module.get_active_tab = lambda bundle_id: ("https://private.example/", "incognito")
    _run_one_poll_tick(tracker)

    assert emitted == [("site", "end", "youtube.com")]


def test_focus_events_are_suppressed_when_disabled(scoped_agent_module):
    tracker = scoped_agent_module.ActivityTracker()
    emitted = []
    tracker.emit = lambda kind, value, subject=None: emitted.append((kind, value, subject))

    tracker.on_activate("Terminal", "com.apple.Terminal")
    tracker.on_activate("Safari", "com.apple.Safari")

    assert emitted == []
