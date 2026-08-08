"""Assembling a day into a single chronological feed.

Days are a presentation concern only. Segmentation knows nothing about calendar
boundaries — a stay that runs from Tuesday evening to Wednesday morning is one
stay — so the splitting happens here, where it can be done without destroying the
underlying record.

An item crossing midnight is clipped to the day being viewed and carries the
fraction of itself that fell inside, so a drive home at 00:30 contributes only its
own share of distance to each day's totals rather than being counted twice or
dropped entirely.

The reference timezone comes from the data itself: stays are stamped with the
zone of their own coordinates, so a day viewed while travelling uses the zone the
user was actually in rather than a fixed account setting.
"""

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "UTC"

# Kinds whose two value_text states form a start/end pair. geofence is
# included so OwnTracks enter/leave events get the same range treatment as
# Shortcuts-sourced signals, and session/focus/site do the same for the
# MacBook daemon. Deliberately generic: a future kind is a new entry here.
_RANGE_KINDS: dict[str, tuple[str, str]] = {
    "app": ("open", "close"),
    "wifi": ("connected", "disconnected"),
    "carplay": ("connected", "disconnected"),
    "geofence": ("enter", "leave"),
    "session": ("unlock", "lock"),
    "focus": ("start", "end"),
    "site": ("start", "end"),
    "sleep": ("start", "end"),
}

# Kinds where at most one subject is ever open per device, unlike e.g. "app"
# where two iPhone apps can legitimately be open at once. These pair on
# (device, kind) alone: a "start" implicitly closes whatever subject was
# previously open, rather than requiring an explicit end for that subject.
# focus always has exactly one frontmost app, so the MacBook daemon never
# sends an explicit "end" at all. site is the same except when a tracked
# browser loses focus entirely, which still sends one explicit "end" since
# there's no next site ping to infer that boundary from.
_EXCLUSIVE_KINDS = {"focus", "site"}

# Bounds how far before a day's start to look for an unmatched "start" ping
# that might still be open (e.g. connected to CarPlay just before midnight on
# a long drive), keeping the query cheap regardless of how much event history
# has accumulated; none of the current signal kinds realistically stay open
# longer than this.
EVENT_LOOKBACK_SECONDS = 3 * 24 * 3600

# The MacBook daemon sends a "session"/"heartbeat" every minute while
# unlocked (see macos/agent.py). Any range still open when heartbeats for its
# device go stale -- an "unlock" with no matching "lock", or a focus/site
# range still open when the machine stopped reporting -- gets closed at the
# last heartbeat instead of reading as still going on: an unclean end
# (crash, dead battery, lost network, going to sleep) is not the same as
# genuinely still ongoing. ~2.5x the daemon's 60s cadence, allowing one
# missed beat plus jitter.
HEARTBEAT_STALE_SECONDS = 150

# What's frontmost while the screen is locked -- not something the user is
# doing, so it isn't shown as a focus block at all; the gap it leaves behind
# already reads as "laptop not in use".
_HIDDEN_FOCUS_SUBJECTS = {"loginwindow"}

# A focus range shorter than this, sandwiched between two ranges of the same
# subject, is treated as a glance away and back rather than a real switch --
# e.g. checking a command's output in iTerm2 mid-edit in Code. Only merges
# when the subject on both sides matches; a genuinely different app taking
# focus, however briefly, still gets its own block.
FOCUS_BLIP_SECONDS = 1


def default_timezone(conn: sqlite3.Connection, device: str | None = None) -> str:
    """The zone of the most recent stay, which is where the user last was."""
    query = "SELECT tz FROM stays WHERE tz IS NOT NULL"
    params: list[Any] = []
    if device:
        query += " AND device = ?"
        params.append(device)
    query += " ORDER BY end_ts DESC LIMIT 1"

    row = conn.execute(query, params).fetchone()
    return row["tz"] if row and row["tz"] else DEFAULT_TZ


def day_bounds(day: str, tz_name: str) -> tuple[int, int]:
    """UTC bounds of a local calendar day.

    The end is built from the next date's midnight rather than by adding 24
    hours, so days that gain or lose an hour to a clock change still cover
    exactly midnight to midnight.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(DEFAULT_TZ)

    start_date = date.fromisoformat(day)
    end_date = start_date + timedelta(days=1)

    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _clip(
    item_start: int, item_end: int, day_start: int, day_end: int
) -> tuple[int, int, float] | None:
    """Portion of an item falling inside the day, and what fraction that is."""
    low = max(item_start, day_start)
    high = min(item_end, day_end)
    if high <= low:
        return None
    total = item_end - item_start
    share = (high - low) / total if total > 0 else 1.0
    return low, high, share


def _local_date(ts: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(DEFAULT_TZ)
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def _stay_anchor(stay: sqlite3.Row) -> int:
    """Same anchor `patch_stay()` writes a note under — see app/main.py."""
    return ((stay["start_ts"] + stay["end_ts"]) // 2 // 300) * 300


def _notes_by_anchor(
    conn: sqlite3.Connection, stays: list[sqlite3.Row]
) -> dict[tuple[str, int], str]:
    """Notes for this day's stays, keyed the same way `patch_stay()` writes them."""
    if not stays:
        return {}
    devices = {s["device"] for s in stays}
    anchors = {_stay_anchor(s) for s in stays}
    placeholders = ",".join("?" * len(devices))
    rows = conn.execute(
        f"""
        SELECT device, anchor_ts, note FROM stay_notes
        WHERE device IN ({placeholders}) AND anchor_ts IN ({",".join("?" * len(anchors))})
        """,
        [*devices, *anchors],
    ).fetchall()
    return {(r["device"], r["anchor_ts"]): r["note"] for r in rows}


def _event_range(
    dev: str | None,
    kind: str,
    open_row: sqlite3.Row,
    end_ts: int | None,
    end_id: int | None,
) -> dict[str, Any]:
    return {
        "device": dev,
        "kind": kind,
        "subject": open_row["subject"],
        "start_ts": open_row["ts"],
        "end_ts": end_ts,
        "ongoing": end_ts is None,
        "start_id": open_row["id"],
        "end_id": end_id,
    }


def _append_range(ranges: list[dict[str, Any]], new: dict[str, Any]) -> None:
    """Append a closed range, folding it into the previous one if the range
    between them was a same-subject blip under FOCUS_BLIP_SECONDS -- e.g.
    checking iTerm2 mid-edit in Code and coming straight back. Runs inline in
    the same sweep that builds ranges from events, not as a second pass over
    the result."""
    if (
        new["kind"] == "focus"
        and len(ranges) >= 2
        and ranges[-1]["end_ts"] is not None
        and ranges[-1]["end_ts"] - ranges[-1]["start_ts"] < FOCUS_BLIP_SECONDS
        and ranges[-1]["end_ts"] == new["start_ts"]
        and ranges[-2]["end_ts"] == ranges[-1]["start_ts"]
        and ranges[-2]["subject"] == new["subject"]
    ):
        ranges.pop()
        ranges[-1]["end_ts"] = new["end_ts"]
        ranges[-1]["end_id"] = new["end_id"]
        ranges[-1]["ongoing"] = new["ongoing"]
    else:
        ranges.append(new)


def _pair_events(
    conn: sqlite3.Connection, day_start: int, day_end: int, device: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn raw start/end pings into ranges.

    Everything in the events table is a point observation — Shortcuts reports
    each transition as it happens. Pairing turns that into a duration here, on
    read, the same way segmentation happens outside of points: the raw layer
    stays a dumb, replayable log of pings.

    Most kinds match same-subject start/end pings, grouped by (device, kind,
    subject) — several subjects can be open at once (e.g. two iPhone apps).
    Kinds in _EXCLUSIVE_KINDS group by (device, kind) alone instead: a "start"
    implicitly closes whichever subject was previously open.

    "session"/"heartbeat" rows are pulled out separately rather than paired at
    all — they're a liveness signal, not a state transition — and used
    afterward to close out any range still open once its device's heartbeats
    go stale, and "focus"/"loginwindow" ranges (what's frontmost while the
    screen is locked) are dropped outright, so locked/unreachable time reads
    as a gap rather than as usage.
    """
    params: list[Any] = [day_start - EVENT_LOOKBACK_SECONDS, day_end]
    query = "SELECT * FROM events WHERE ts >= ? AND ts < ?"
    if device:
        query += " AND device = ?"
        params.append(device)
    query += " ORDER BY ts"
    rows = conn.execute(query, params).fetchall()

    exclusive_groups: dict[tuple[str | None, str], list[sqlite3.Row]] = {}
    subject_groups: dict[tuple[str | None, str, str | None], list[sqlite3.Row]] = {}
    last_heartbeat: dict[str | None, int] = {}
    for row in rows:
        if row["kind"] == "session" and row["value_text"] == "heartbeat":
            dev = row["device"]
            if dev not in last_heartbeat or row["ts"] > last_heartbeat[dev]:
                last_heartbeat[dev] = row["ts"]
        elif row["kind"] in _EXCLUSIVE_KINDS:
            exclusive_groups.setdefault((row["device"], row["kind"]), []).append(row)
        else:
            subject_groups.setdefault(
                (row["device"], row["kind"], row["subject"]), []
            ).append(row)

    ranges: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []

    for (dev, kind), events in exclusive_groups.items():
        start_value, end_value = _RANGE_KINDS[kind]
        open_row = None
        group_ranges: list[dict[str, Any]] = []
        for row in events:
            value = row["value_text"]
            if value == start_value:
                if open_row is not None:
                    if row["subject"] == open_row["subject"]:
                        continue  # repeat start for the same subject -- noise
                    _append_range(
                        group_ranges, _event_range(dev, kind, open_row, row["ts"], row["id"])
                    )
                open_row = row
            elif value == end_value:
                if open_row is not None:
                    _append_range(
                        group_ranges, _event_range(dev, kind, open_row, row["ts"], row["id"])
                    )
                    open_row = None
                else:
                    points.append({**dict(row), "flagged": True})
            else:
                points.append({**dict(row), "flagged": True})

        if open_row is not None:
            _append_range(group_ranges, _event_range(dev, kind, open_row, None, None))

        ranges.extend(group_ranges)

    for (dev, kind, subject), events in subject_groups.items():
        pair = _RANGE_KINDS.get(kind)
        if pair is None:
            points.extend({**dict(r), "flagged": False} for r in events)
            continue

        start_value, end_value = pair
        open_row = None
        for row in events:
            value = row["value_text"]
            if value == start_value:
                if open_row is None:  # a repeat "start" while already open is noise
                    open_row = row
            elif value == end_value and open_row is not None:
                ranges.append(_event_range(dev, kind, open_row, row["ts"], row["id"]))
                open_row = None
            else:
                # an end with no open start, or an unrecognised value_text --
                # exactly the "how often does this fail to pair" signal.
                points.append({**dict(row), "flagged": True})

        if open_row is not None:
            ranges.append(_event_range(dev, kind, open_row, None, None))

    now = time.time()
    for r in ranges:
        if not r["ongoing"]:
            continue
        last = last_heartbeat.get(r["device"])
        if last is None or last <= r["start_ts"] or now - last <= HEARTBEAT_STALE_SECONDS:
            continue  # no heartbeat since it opened, or still arriving -- genuinely ongoing
        r["end_ts"] = last
        r["ongoing"] = False

    ranges = [
        r for r in ranges if not (r["kind"] == "focus" and r["subject"] in _HIDDEN_FOCUS_SUBJECTS)
    ]

    return ranges, points


def assemble_day(
    conn: sqlite3.Connection,
    day: str,
    device: str | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """Stays and trips for one local day, interleaved, with a summary."""
    tz_name = tz_name or default_timezone(conn, device)
    day_start, day_end = day_bounds(day, tz_name)

    params: list[Any] = [day_end, day_start]
    if device:
        params.append(device)

    stays = conn.execute(
        """
        SELECT s.*, p.name AS place_name, a.name AS area_name
        FROM stays s
        LEFT JOIN places p ON p.id = s.place_id
        LEFT JOIN areas  a ON a.id = s.area_id
        WHERE s.start_ts < ? AND s.end_ts > ?
        """
        + (" AND s.device = ?" if device else "")
        + " ORDER BY s.start_ts",
        params,
    ).fetchall()

    trips = conn.execute(
        """
        SELECT * FROM trips
        WHERE start_ts < ? AND end_ts > ?
        """
        + (" AND device = ?" if device else "")
        + " ORDER BY start_ts",
        params,
    ).fetchall()

    items: list[dict[str, Any]] = []
    notes = _notes_by_anchor(conn, stays)

    for stay in stays:
        clipped = _clip(stay["start_ts"], stay["end_ts"], day_start, day_end)
        if clipped is None:
            continue
        visible_start, visible_end, share = clipped
        items.append(
            {
                "type": "stay",
                "id": stay["id"],
                "device": stay["device"],
                "start_ts": stay["start_ts"],
                "end_ts": stay["end_ts"],
                "visible_start_ts": visible_start,
                "visible_end_ts": visible_end,
                "duration_s": stay["end_ts"] - stay["start_ts"],
                "visible_duration_s": visible_end - visible_start,
                "share": share,
                "lat": stay["center_lat"],
                "lon": stay["center_lon"],
                "radius_m": stay["radius_m"],
                "point_count": stay["point_count"],
                "confidence": stay["confidence"],
                "confidence_breakdown": (
                    json.loads(stay["confidence_breakdown"])
                    if stay["confidence_breakdown"]
                    else {}
                ),
                "had_gap": bool(stay["had_gap"]),
                "tz": stay["tz"],
                "place_id": stay["place_id"],
                "area_id": stay["area_id"],
                "name": stay["area_name"] or stay["place_name"],
                "note": notes.get((stay["device"], _stay_anchor(stay))),
                "continuation_of": (
                    _local_date(stay["start_ts"], tz_name)
                    if stay["start_ts"] < day_start
                    else None
                ),
            }
        )

    for trip in trips:
        clipped = _clip(trip["start_ts"], trip["end_ts"], day_start, day_end)
        if clipped is None:
            continue
        visible_start, visible_end, share = clipped
        items.append(
            {
                "type": "trip",
                "id": trip["id"],
                "device": trip["device"],
                "start_ts": trip["start_ts"],
                "end_ts": trip["end_ts"],
                "visible_start_ts": visible_start,
                "visible_end_ts": visible_end,
                "duration_s": trip["end_ts"] - trip["start_ts"],
                "visible_duration_s": visible_end - visible_start,
                "share": share,
                # Only this day's portion counts toward its totals.
                "distance_m": trip["distance_m"] * share,
                "total_distance_m": trip["distance_m"],
                "point_count": trip["point_count"],
                "avg_speed": trip["avg_speed"],
                "max_speed": trip["max_speed"],
                "mode": trip["mode"],
                "from_stay_id": trip["from_stay_id"],
                "to_stay_id": trip["to_stay_id"],
                "continuation_of": (
                    _local_date(trip["start_ts"], tz_name)
                    if trip["start_ts"] < day_start
                    else None
                ),
            }
        )

    event_ranges, event_points = _pair_events(conn, day_start, day_end, device)

    now_ts = int(time.time())
    for r in event_ranges:
        # A genuinely open range (no end ping yet) has only actually happened up
        # to now, not to the end of the calendar day -- clipping it to day_end
        # instead would draw it running into hours that haven't occurred yet on
        # a day still in progress. On a past day now_ts > day_end, so this is a
        # no-op and it clips to day_end exactly as before.
        clip_end = r["end_ts"] if r["end_ts"] is not None else min(day_end, now_ts)
        clipped = _clip(r["start_ts"], clip_end, day_start, day_end)
        if clipped is None:
            continue
        visible_start, visible_end, _ = clipped
        items.append(
            {
                "type": "event",
                "shape": "range",
                "device": r["device"],
                "kind": r["kind"],
                "subject": r["subject"],
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "visible_start_ts": visible_start,
                "visible_end_ts": visible_end,
                "visible_duration_s": visible_end - visible_start,
                "ongoing": r["ongoing"],
                "continuation_of": (
                    _local_date(r["start_ts"], tz_name) if r["start_ts"] < day_start else None
                ),
            }
        )

    for p in event_points:
        if not (day_start <= p["ts"] < day_end):
            continue  # a flagged point from the lookback window, not this day
        items.append(
            {
                "type": "event",
                "shape": "point",
                "device": p["device"],
                "kind": p["kind"],
                "subject": p["subject"],
                "value_text": p["value_text"],
                "value_num": p["value_num"],
                "visible_start_ts": p["ts"],
                "visible_end_ts": p["ts"],
                "flagged": p["flagged"],
            }
        )

    items.sort(key=lambda item: item["visible_start_ts"])

    moving = sum(i["visible_duration_s"] for i in items if i["type"] == "trip")
    stationary = sum(i["visible_duration_s"] for i in items if i["type"] == "stay")
    distance = sum(i["distance_m"] for i in items if i["type"] == "trip")

    return {
        "date": day,
        "tz": tz_name,
        "device": device,
        "start_ts": day_start,
        "end_ts": day_end,
        "items": items,
        "summary": {
            "distance_m": distance,
            "time_moving_s": moving,
            "time_stationary_s": stationary,
            "stay_count": sum(1 for i in items if i["type"] == "stay"),
            "trip_count": sum(1 for i in items if i["type"] == "trip"),
            "event_count": sum(1 for i in items if i["type"] == "event"),
            "first_ts": items[0]["visible_start_ts"] if items else None,
            "last_ts": max((i["visible_end_ts"] for i in items), default=None),
        },
    }


def stats(
    conn: sqlite3.Connection,
    from_ts: int | None = None,
    to_ts: int | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Totals over a range, computed on read rather than from a cache.

    Precomputed monthly rollups drift out of sync with the points they summarise;
    at this data volume the query is cheap enough not to need one.
    """
    where: list[str] = []
    params: list[Any] = []
    point_where: list[str] = []
    point_params: list[Any] = []

    if from_ts is not None:
        where.append("end_ts > ?")
        params.append(from_ts)
        point_where.append("ts >= ?")
        point_params.append(from_ts)
    if to_ts is not None:
        where.append("start_ts < ?")
        params.append(to_ts)
        point_where.append("ts < ?")
        point_params.append(to_ts)
    if device:
        where.append("device = ?")
        params.append(device)
        point_where.append("device = ?")
        point_params.append(device)

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    point_clause = f" WHERE {' AND '.join(point_where)}" if point_where else ""

    trips = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(distance_m), 0) AS distance, "
        f"COALESCE(SUM(end_ts - start_ts), 0) AS duration, MAX(max_speed) AS top_speed "
        f"FROM trips{clause}",
        params,
    ).fetchone()

    stays = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(end_ts - start_ts), 0) AS duration "
        f"FROM stays{clause}",
        params,
    ).fetchone()

    points = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(anomaly), 0) AS flagged FROM points{point_clause}",
        point_params,
    ).fetchone()

    return {
        "trips": trips["n"],
        "distance_m": trips["distance"],
        "time_moving_s": trips["duration"],
        "max_speed": trips["top_speed"],
        "stays": stays["n"],
        "time_stationary_s": stays["duration"],
        "points": points["n"],
        "points_flagged": points["flagged"],
        "places": conn.execute("SELECT COUNT(*) AS n FROM places").fetchone()["n"],
        "areas": conn.execute("SELECT COUNT(*) AS n FROM areas").fetchone()["n"],
    }
