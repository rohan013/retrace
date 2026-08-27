"""Collapsing a day's overlapping streams into one partition of the clock.

The day view's streams overlap by construction: sleep runs through whatever stay
was in progress, a `site` range sits inside its `focus` range, and two phone apps
can be open at once. Summed naively they exceed the day several times over -- on a
representative day, 45 hours of it.

A breakdown has to answer "where did the 24 hours go", so every instant is assigned
exactly one place and exactly one activity, and the totals close. Two sweeps over
the same boundary cuts do it: at each interval, the covering place wins by being the
only one, and the covering activity wins on priority.

This works from an assembled day's `items`, not from the database, so it inherits
midnight clipping, the focus blip merge, the heartbeat clamp and the `loginwindow`
filter rather than reproducing any of them.
"""

from typing import Any

from . import config

# Places that name a condition rather than somewhere you were.
MOVING = "Moving"
NO_LOCATION = "No location"
UNNAMED = "Unnamed"

# Activities that name the absence of a tracked one.
OTHER = "Other"
UNTRACKED = "Untracked"

# Which stream wins an instant that several of them cover. `focus` is a real
# "this is frontmost now" observation, so the MacBook outranks the phone, whose
# `app` ranges can sit stale for hours. `site` outranks `focus` because a site is
# always inside a browser's focus range and is the more specific of the two.
_ACTIVITY_PRIORITY: dict[str, int] = {
    "sleep": 0,
    "site": 1,
    "focus": 2,
    "app": 3,
}

# The named things worth their own slice, per kind. Subjects are matched against
# the normalised form below; anything else in a tracked kind is _KIND_DEFAULT.
_TRACKED_SUBJECTS: dict[str, dict[str, str]] = {
    "site": {"reddit.com": "Reddit", "youtube.com": "YouTube"},
    "app": {"reddit": "Reddit", "youtube": "YouTube"},
}

# What an untracked subject in a tracked kind becomes. `sleep` carries no subject
# at all -- WHOOP sends only start/end -- so its default is the whole story.
_KIND_DEFAULT: dict[str, str] = {
    "sleep": "Sleep",
    "site": OTHER,
    "focus": OTHER,
    "app": OTHER,
}


def _normalise(subject: str | None) -> str:
    s = str(subject or "").strip().lower()
    return s[4:] if s.startswith("www.") else s


def _domain_keys(subject: str) -> list[str]:
    """"m.reddit.com" -> ["m.reddit.com", "reddit.com"], so a subdomain still
    lands on its site's slice -- the same widening `subjectColor` does in the UI."""
    parts = subject.split(".")
    keys = [subject]
    for i in range(1, max(0, len(parts) - 1)):
        keys.append(".".join(parts[i:]))
    return keys


def _activity_label(kind: str, subject: str | None) -> str:
    tracked = _TRACKED_SUBJECTS.get(kind)
    if tracked:
        key = _normalise(subject)
        for candidate in _domain_keys(key) if "." in key else [key]:
            if candidate in tracked:
                return tracked[candidate]
    return _KIND_DEFAULT[kind]


def _is_moving(trip: dict[str, Any], min_fixes_per_hour: float) -> bool:
    """Whether a trip is a journey or a hole in the record.

    A trip is whatever sits between two stays, so a phone that goes dark for a
    day and comes back somewhere else produces one enormous trip. Reporting that
    as `Moving` claims a drive that did not happen, and on real data it is the
    largest slice of the ring on days that also contain a full night's sleep.

    Fix density separates the two cleanly and with room to spare: journeys run
    tens to hundreds of fixes an hour, while the holes run a fix or two across
    tens of hours.
    """
    hours = trip["duration_s"] / 3600
    if hours <= 0:
        return True
    return (trip["point_count"] / hours) >= min_fixes_per_hour


def _place_spans(
    items: list[dict[str, Any]], min_fixes_per_hour: float
) -> list[tuple[int, int, str, dict[str, Any]]]:
    spans = []
    for item in items:
        if item["type"] == "stay":
            label = item.get("name") or UNNAMED
            identity = {
                "place_id": item.get("place_id"),
                "area_id": item.get("area_id"),
                "lat": item.get("lat"),
                "lon": item.get("lon"),
            }
        elif item["type"] == "trip":
            label = MOVING if _is_moving(item, min_fixes_per_hour) else NO_LOCATION
            identity = {}
        else:
            continue
        start, end = item["visible_start_ts"], item["visible_end_ts"]
        if end > start:
            spans.append((start, end, label, identity))
    return spans


def _activity_spans(items: list[dict[str, Any]]) -> list[tuple[int, int, int, str]]:
    spans = []
    for item in items:
        if item["type"] != "event" or item.get("shape") != "range":
            continue
        priority = _ACTIVITY_PRIORITY.get(item["kind"])
        if priority is None:
            continue
        start, end = item["visible_start_ts"], item["visible_end_ts"]
        if end > start:
            spans.append((start, end, priority, _activity_label(item["kind"], item.get("subject"))))
    return spans


def _sweep(spans: list[tuple], cuts: list[int], pick):
    """Walk the cuts once, keeping the spans covering the current interval.

    Testing every span at every cut is quadratic, and a busy MacBook day is a
    thousand spans against two thousand cuts. Spans enter in start order and
    leave as the sweep passes their end, so only the handful genuinely
    overlapping the current instant is ever examined.
    """
    ordered = sorted(spans, key=lambda s: s[0])
    active: list[tuple] = []
    nxt = 0
    for low, high in zip(cuts, cuts[1:]):
        if high <= low:
            continue
        while nxt < len(ordered) and ordered[nxt][0] <= low:
            active.append(ordered[nxt])
            nxt += 1
        active = [s for s in active if s[1] > low]
        yield low, high, pick(active) if active else None


def day_breakdown(
    items: list[dict[str, Any]], day_start: int, day_end: int
) -> dict[str, Any]:
    """One day partitioned into (place, activity) pairs that total the day.

    Both rollups sum to `day_end - day_start` exactly, including on a DST day
    that is 23 or 25 hours long, and including a day holding nothing at all --
    which reads as untracked time somewhere unknown rather than as an empty
    chart.
    """
    min_fixes_per_hour = config.BREAKDOWN_TRIP_MIN_FIXES_PER_HOUR
    places = _place_spans(items, min_fixes_per_hour)
    activities = _activity_spans(items)

    cuts = {day_start, day_end}
    for start, end, *_ in places:
        cuts.update((start, end))
    for start, end, *_ in activities:
        cuts.update((start, end))
    ordered_cuts = sorted(c for c in cuts if day_start <= c <= day_end)

    # Overlapping stays -- two devices in the same hour -- are settled by taking
    # the one that started most recently, the same rule the activity sweep uses.
    place_at = _sweep(places, ordered_cuts, lambda a: max(a, key=lambda s: s[0]))
    # Lowest priority number wins; ties within a stream go to whichever started
    # most recently, so a stale range never suppresses what actually replaced it.
    activity_at = _sweep(activities, ordered_cuts, lambda a: min(a, key=lambda s: (s[2], -s[0])))

    grid: dict[tuple[str, str], int] = {}
    place_totals: dict[str, int] = {}
    place_identity: dict[str, dict[str, Any]] = {}
    activity_totals: dict[str, int] = {}

    for (low, high, place), (_, _, activity) in zip(place_at, activity_at):
        seconds = high - low
        place_label = place[2] if place else NO_LOCATION
        activity_label = activity[3] if activity else UNTRACKED
        if place and place_label not in place_identity and place[3]:
            place_identity[place_label] = place[3]
        grid[(place_label, activity_label)] = grid.get((place_label, activity_label), 0) + seconds
        place_totals[place_label] = place_totals.get(place_label, 0) + seconds
        activity_totals[activity_label] = activity_totals.get(activity_label, 0) + seconds

    total = day_end - day_start
    share = lambda seconds: (seconds / total) if total > 0 else 0.0  # noqa: E731

    place_rows = []
    for label, seconds in sorted(place_totals.items(), key=lambda kv: -kv[1]):
        inner = sorted(
            ((a, s) for (p, a), s in grid.items() if p == label), key=lambda kv: -kv[1]
        )
        place_rows.append(
            {
                "label": label,
                "seconds": seconds,
                "share": share(seconds),
                **place_identity.get(label, {}),
                "activities": [
                    {"label": a, "seconds": s, "share": share(s)} for a, s in inner
                ],
            }
        )

    return {
        "total_s": total,
        "places": place_rows,
        "activities": [
            {"label": label, "seconds": seconds, "share": share(seconds)}
            for label, seconds in sorted(activity_totals.items(), key=lambda kv: -kv[1])
        ],
    }
