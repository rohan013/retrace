"""The location-freshness alert.

Two failure shapes matter more than the happy path. One is saying nothing when
tracking has actually stopped, which is the whole problem this exists to fix.
The other is saying it over and over: the check runs every five minutes, so any
condition that stays true and is not suppressed becomes hundreds of messages
about one outage.
"""

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("freshness_check", SCRIPTS / "freshness_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["freshness_check"] = module
    spec.loader.exec_module(module)
    return module


freshness = _load()

NOW = 1780304400  # 2026-06-01 09:00:00 UTC
STALE_AFTER = 600  # ten minutes


def decide(last_fix, alerted_for=None, now=NOW, stale_after=STALE_AFTER):
    return freshness.decide(now, last_fix, alerted_for, stale_after, "iphone")


# -- when to speak -----------------------------------------------------------


def test_a_recent_fix_says_nothing():
    assert decide(NOW - 60).action == "nothing"


def test_silence_past_the_threshold_alerts():
    decision = decide(NOW - 900)
    assert decision.action == "alert"
    assert "15 min" in decision.message
    assert "iphone" in decision.message


def test_the_threshold_is_exclusive():
    """Exactly at the threshold is not yet an outage; a second past it is."""
    assert decide(NOW - STALE_AFTER + 1).action == "nothing"
    assert decide(NOW - STALE_AFTER).action == "alert"


def test_the_threshold_is_configurable():
    assert decide(NOW - 900, stale_after=1200).action == "nothing"
    assert decide(NOW - 900, stale_after=300).action == "alert"


# -- saying it only once -----------------------------------------------------


def test_the_same_outage_is_not_reported_twice():
    """The marker holds the last_fix the outstanding alert was about. Without
    this, a 39-hour silence checked every five minutes is 468 messages."""
    last_fix = NOW - 900
    first = decide(last_fix)
    assert first.action == "alert"
    assert first.mark == last_fix
    assert decide(last_fix, alerted_for=first.mark).action == "nothing"


def test_a_long_outage_stays_quiet_however_long_it_runs():
    last_fix = NOW - 39 * 3600
    assert decide(last_fix, alerted_for=last_fix, now=NOW).action == "nothing"
    assert decide(last_fix, alerted_for=last_fix, now=NOW + 86400).action == "nothing"


def test_a_fix_after_an_alert_reports_recovery_and_clears_the_marker():
    """The hole is measured from the last fix before it to the first one after,
    not from when the alert happened to be sent."""
    decision = decide(NOW, alerted_for=NOW - 6 * 3600)
    assert decision.action == "recovered"
    assert "6h dark" in decision.message
    assert decision.mark is None


def test_a_second_outage_after_a_recovery_alerts_again():
    """Recovery clears the marker, so the next outage is a new one."""
    recovered = decide(NOW - 30, alerted_for=NOW - 6 * 3600)
    assert recovered.mark is None
    assert decide(NOW - 900, alerted_for=None).action == "alert"


def test_recovery_is_reported_before_a_fresh_outage_can_be():
    """A fix arrived and the device went quiet again between two runs. Recovery
    wins, so the marker is cleared and the new outage alerts on the next run
    rather than being mistaken for the old one."""
    decision = decide(NOW - 900, alerted_for=NOW - 20 * 3600)
    assert decision.action == "recovered"
    assert decision.mark is None


# -- the configuration mistake that would never stop -------------------------


def test_a_device_that_has_never_reported_is_not_an_outage():
    """A misspelled STALE_ALERT_DEVICE has no fixes at all. Treating that as an
    outage means a message every five minutes forever."""
    assert decide(None).action == "nothing"
    assert decide(None, alerted_for=NOW - 3600).action == "nothing"


# -- durations, as read on a phone -------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0 min"), (59, "0 min"), (600, "10 min"), (3599, "59 min"),
     (3600, "1h"), (3900, "1h 5m"), (86399, "23h 59m"), (86400, "1d"),
     (140400, "1d 15h")],
)
def test_durations_read_naturally(seconds, expected):
    assert freshness.humanise(seconds) == expected


# -- delivery ----------------------------------------------------------------


def test_the_message_is_handed_over_on_stdin(tmp_path):
    sink = tmp_path / "received.txt"
    script = tmp_path / "deliver.sh"
    script.write_text(f'#!/bin/sh\ncat > "{sink}"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    freshness.send(str(script), "location tracking stopped")
    assert sink.read_text() == "location tracking stopped"


def test_a_delivery_command_is_never_run_through_a_shell(tmp_path):
    """The value comes from .env, so treating it as a shell string would make
    that file a place arbitrary commands get composed."""
    marker = tmp_path / "should-not-exist"
    with pytest.raises(FileNotFoundError):
        freshness.send(f"/bin/echo hi; touch {marker}", "message")
    assert not marker.exists()


def test_a_failed_delivery_raises_so_the_marker_is_not_written(tmp_path):
    """main() writes the marker only after send() returns. A command that exits
    non-zero has to raise, or the outage is recorded as reported and never
    mentioned again."""
    script = tmp_path / "broken.sh"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        freshness.send(str(script), "message")
