#!/usr/bin/env python3
"""Generate a plausible day of movement and post it to a running tracker.

Useful for exercising the UI before the phone is set up, and for sanity-checking
threshold changes against a known-shape day rather than waiting for real data.

    scripts/synth_day.py --days 7 --url http://127.0.0.1:8420
"""

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HOME = (51.5074, -0.1278)


def offset_m(origin, north_m, east_m):
    lat, lon = origin
    return (
        lat + north_m / 111_320.0,
        lon + east_m / (111_320.0 * math.cos(math.radians(lat))),
    )


OFFICE = offset_m(HOME, 4_200, 3_100)
GYM = offset_m(HOME, -900, 1_400)


class Day:
    def __init__(self, start_ts, device, rng):
        self.ts = start_ts
        self.pos = HOME
        self.device = device
        self.rng = rng
        self.points = []

    def _emit(self, pos, accuracy, speed=None):
        self.points.append(
            {
                "device": self.device,
                "ts": int(self.ts),
                "lat": pos[0],
                "lon": pos[1],
                "accuracy": round(accuracy, 1),
                "speed_mps": speed,
            }
        )

    def stay(self, hours, interval=180, jitter=9.0, accuracy=12.0):
        end = self.ts + hours * 3600
        centre = self.pos
        while self.ts < end:
            self._emit(
                offset_m(
                    centre,
                    self.rng.uniform(-jitter, jitter),
                    self.rng.uniform(-jitter, jitter),
                ),
                accuracy * self.rng.uniform(0.6, 1.8),
                0.0,
            )
            self.ts += interval * self.rng.uniform(0.8, 1.2)
        self.pos = centre
        return self

    def travel(self, destination, speed, interval=20, accuracy=10.0):
        start = self.pos
        dlat = destination[0] - start[0]
        dlon = destination[1] - start[1]
        metres = math.hypot(dlat * 111_320, dlon * 111_320 * math.cos(math.radians(start[0])))
        steps = max(2, int(metres / speed / interval))
        for step in range(1, steps + 1):
            f = step / steps
            wobble = self.rng.uniform(-0.00002, 0.00002)
            self._emit(
                (start[0] + dlat * f + wobble, start[1] + dlon * f + wobble),
                accuracy * self.rng.uniform(0.5, 2.0),
                speed * self.rng.uniform(0.7, 1.2),
            )
            self.ts += interval
        self.pos = destination
        return self

    def quiet(self, hours):
        """Silence — the phone stopped reporting."""
        self.ts += hours * 3600
        return self


def build_day(start_ts, device, rng, weekday=True):
    day = Day(start_ts, device, rng)
    if weekday:
        day.stay(7.5, interval=300)                       # asleep at home
        day.travel(OFFICE, speed=11)                      # commute in
        day.stay(4.0)
        day.quiet(1.0)                                    # phone idle at desk
        day.stay(3.5)
        day.travel(GYM, speed=9)
        day.stay(1.2, interval=120)
        day.travel(HOME, speed=10)
        day.stay(3.0, interval=300)
    else:
        day.stay(9.5, interval=300)
        day.travel(offset_m(HOME, 1_800, -2_400), speed=1.3, interval=30)  # walk
        day.stay(1.5, interval=120)
        day.travel(HOME, speed=1.3, interval=30)
        day.stay(6.0, interval=300)
    return day.points


def post(url, token, points, chunk=500):
    sent = 0
    for i in range(0, len(points), chunk):
        body = json.dumps({"points": points[i : i + chunk]}).encode()
        request = urllib.request.Request(
            f"{url}/api/v1/locations",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(request) as response:
            sent += json.loads(response.read()).get("accepted", 0)
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8420")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--device", default="synthetic")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--token", default=os.environ.get("INGEST_TOKEN"))
    args = parser.parse_args()

    if not args.token:
        sys.exit("Set INGEST_TOKEN or pass --token")

    rng = random.Random(args.seed)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total = 0
    for back in range(args.days, 0, -1):
        start = today - timedelta(days=back)
        points = build_day(start.timestamp(), args.device, rng, weekday=start.weekday() < 5)
        total += post(args.url, args.token, points)
        print(f"{start.date()}  {len(points):>5} points")

    print(f"\naccepted {total} points; rebuilding derived layer")
    request = urllib.request.Request(f"{args.url}/api/v1/reprocess", method="POST", data=b"")
    with urllib.request.urlopen(request) as response:
        print(json.dumps(json.loads(response.read()), indent=2))


if __name__ == "__main__":
    main()
