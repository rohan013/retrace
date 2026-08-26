#!/usr/bin/env python3
"""Alert when location fixes stop arriving.

OwnTracks on iOS stops reporting and says nothing about it. The app is not
relaunched by the system once it goes, so tracking simply ends until it is
opened by hand -- and the only evidence is a hole in the record found days
later. Over one recent three-week stretch that was 270 hours dark, 55% of the
period, the longest single silence 39 hours.

The rule is deliberately plain: no fix from the watched device for
STALE_ALERT_AFTER_MINUTES. A healthy phone reports every few minutes, so a
silence past that is either a dead recorder or a phone that is off, and both
are worth knowing about.

One message per outage, not one per run -- the marker in `state` records which
outage has already been reported, so a 39-hour silence sends one message rather
than hundreds. A second message goes out when fixes resume, saying how long the
hole was.

Delivery is whatever ALERT_COMMAND names: an executable that takes the message
on stdin. Nothing about Telegram, or any other channel, is known here.

    scripts/freshness_check.py               # check, and alert if stale
    scripts/freshness_check.py --dry-run     # print what it would send
    scripts/freshness_check.py --force       # send a test message
"""

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402  -- needs ROOT on sys.path first

# The outage a message has already gone out about, as the `last_fix` value at
# the time. Absent means nothing is outstanding.
ALERT_FIX_TS_KEY = "freshness_alert_fix_ts"

# How long the delivery command gets before it is considered failed. notify.sh
# already retries curl twice with its own 20s timeout, so this only has to stop
# a wedged command from holding the timer's run open indefinitely.
SEND_TIMEOUT_SECONDS = 90


@dataclass(slots=True)
class Decision:
    action: str  # "nothing" | "alert" | "recovered"
    message: str = ""
    mark: int | None = None  # what to write to the marker, None to clear it


def humanise(seconds: int) -> str:
    """Durations here span minutes to days, and are read on a phone."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def decide(
    now: int,
    last_fix: int | None,
    alerted_for: int | None,
    stale_after_s: int,
    device: str,
) -> Decision:
    """What, if anything, to say. Pure -- no database, no network.

    `last_fix` is when the watched device last reported, `alerted_for` is the
    `last_fix` value an outstanding alert was about.
    """
    if last_fix is None:
        # A device that has never reported is a configuration mistake, not an
        # outage. Alerting would mean a message every run, forever.
        return Decision("nothing")

    if alerted_for is not None and last_fix > alerted_for:
        dark = last_fix - alerted_for
        return Decision(
            "recovered",
            f"location tracking resumed — {humanise(dark)} dark.",
            mark=None,
        )

    silent = now - last_fix
    if silent < stale_after_s:
        return Decision("nothing")

    if alerted_for == last_fix:
        return Decision("nothing")  # same outage, already reported

    return Decision(
        "alert",
        f"location tracking stopped — no fix from {device} for {humanise(silent)}.\n"
        "Open OwnTracks to resume.",
        mark=last_fix,
    )


def last_fix_ts(conn, device: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM points WHERE device = ? AND anomaly IS NOT 1",
        (device,),
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


def send(command: str, message: str) -> None:
    """Hand the message to the delivery command on stdin.

    Run as a bare argv, never through a shell: the configured value is a path to
    an executable, and treating it as a shell string would make .env a place
    where arbitrary commands get composed.
    """
    subprocess.run(
        [command],
        input=message,
        text=True,
        check=True,
        timeout=SEND_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the message instead of sending it"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="send a test message regardless of state, to prove the delivery path",
    )
    args = parser.parse_args()

    command = os.environ.get("ALERT_COMMAND", "")
    if not command:
        print("ALERT_COMMAND not set -- fill it in .env first", file=sys.stderr)
        return 1

    device = config.STALE_ALERT_DEVICE
    if not device:
        print(
            "STALE_ALERT_DEVICE not set -- name the device to watch in .env "
            "(see GET /api/v1/devices)",
            file=sys.stderr,
        )
        return 1

    if args.force:
        message = f"retrace freshness check — test message, watching {device}."
        if args.dry_run:
            print(message)
        else:
            send(command, message)
        print("freshness check: test message sent")
        return 0

    now = int(time.time())
    stale_after_s = config.STALE_ALERT_AFTER_MINUTES * 60

    with db.connection() as conn:
        last_fix = last_fix_ts(conn, device)
        raw_marker = db.get_state(conn, ALERT_FIX_TS_KEY)
        alerted_for = int(raw_marker) if raw_marker is not None else None

        decision = decide(now, last_fix, alerted_for, stale_after_s, device)

        if decision.action == "nothing":
            if last_fix is None:
                print(f"freshness check: {device} has never reported a fix")
            elif now - last_fix >= stale_after_s:
                print(
                    f"freshness check: {device} still down, "
                    f"{humanise(now - last_fix)} — already reported"
                )
            else:
                print(f"freshness check: {device} ok, last fix {humanise(now - last_fix)} ago")
            return 0

        if args.dry_run:
            print(f"freshness check: would send —\n{decision.message}")
            return 0

        # Deliver before recording it. A send that fails leaves the marker as it
        # was, so the next run tries again rather than the outage being
        # silently written off as reported.
        send(command, decision.message)

        if decision.mark is None:
            conn.execute("DELETE FROM state WHERE key = ?", (ALERT_FIX_TS_KEY,))
        else:
            db.set_state(conn, ALERT_FIX_TS_KEY, str(decision.mark))

    print(f"freshness check: {decision.action} — {decision.message.splitlines()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
