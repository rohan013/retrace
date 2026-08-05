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

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "UTC"

# Kinds whose two value_text states form a start/end pair. geofence is
# included so OwnTracks enter/leave events get the same range treatment as
# Shortcuts-sourced signals. Deliberately generic: a future kind is a new
# entry here.
_RANGE_KINDS: dict[str, tuple[str, str]] = {
    "app": ("open", "close"),
    "wifi": ("connected", "disconnected"),
    "carplay": ("connected", "disconnected"),
    "geofence": ("enter", "leave"),
}

# Bounds how far before a day's start to look for an unmatched "start" ping
# that might still be open (e.g. connected to CarPlay just before midnight on
# a long drive), keeping the query cheap regardless of how much event history
# has accumulated; none of the current signal kinds realistically stay open
# longer than this.
EVENT_LOOKBACK_SECONDS = 3 * 24 * 3600


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


def _pair_events(
    conn: sqlite3.Connection, day_start: int, day_end: int, device: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn raw start/end pings into ranges, per (device, kind, subject).

    Everything in the events table is a point observation — Shortcuts reports
    each transition as it happens. Pairing turns that into a duration here, on
    read, the same way segmentation happens outside of points: the raw layer
    stays a dumb, replayable log of pings.
    """
    params: list[Any] = [day_start - EVENT_LOOKBACK_SECONDS, day_end]
    query = "SELECT * FROM events WHERE ts >= ? AND ts < ?"
    if device:
        query += " AND device = ?"
        params.append(device)
    query += " ORDER BY ts"
    rows = conn.execute(query, params).fetchall()

    groups: dict[tuple[str | None, str, str | None], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["device"], row["kind"], row["subject"]), []).append(row)

    ranges: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []

    for (dev, kind, subject), events in groups.items():
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
                ranges.append(
                    {
                        "device": dev,
                        "kind": kind,
                        "subject": subject,
                        "start_ts": open_row["ts"],
                        "end_ts": row["ts"],
                        "ongoing": False,
                        "start_id": open_row["id"],
                        "end_id": row["id"],
                    }
                )
                open_row = None
            else:
                # an end with no open start, or an unrecognised value_text --
                # exactly the "how often does this fail to pair" signal.
                points.append({**dict(row), "flagged": True})

        if open_row is not None:
            ranges.append(
                {
                    "device": dev,
                    "kind": kind,
                    "subject": subject,
                    "start_ts": open_row["ts"],
                    "end_ts": None,
                    "ongoing": True,
                    "start_id": open_row["id"],
                    "end_id": None,
                }
            )

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
                "had_gap": bool(stay["had_gap"]),
                "tz": stay["tz"],
                "place_id": stay["place_id"],
                "area_id": stay["area_id"],
                "name": stay["area_name"] or stay["place_name"],
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

    for r in event_ranges:
        clipped = _clip(r["start_ts"], r["end_ts"] or day_end, day_start, day_end)
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
