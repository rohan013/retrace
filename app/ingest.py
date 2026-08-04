"""Writing incoming fixes to the raw layer.

Ingest is deliberately dumb: normalise, deduplicate, store, and record that a
time range needs re-deriving. It makes no judgements about data quality — that
happens later, non-destructively, so a filtering decision can always be revised
by rebuilding rather than by re-collecting data that no longer exists.
"""

import json
import sqlite3
import time
from typing import Any, Iterable

from . import db
from .providers import IngestResult, ParsedEvent, ParsedPoint, ParseResult, Provider
from . import providers

DIRTY_FROM_KEY = "dirty_from_ts"

_POINT_COLUMNS = (
    "device",
    "ts",
    "lat",
    "lon",
    "accuracy",
    "altitude",
    "vertical_accuracy",
    "speed_mps",
    "heading",
    "battery",
    "battery_status",
    "connection",
    "trigger_type",
    "ssid",
    "bssid",
    "pressure",
    "monitoring_mode",
    "in_regions",
    "source",
    "raw",
    "created_at",
)

_INSERT_POINT = (
    f"INSERT OR IGNORE INTO points ({', '.join(_POINT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_POINT_COLUMNS))})"
)

_INSERT_EVENT = """
INSERT OR IGNORE INTO events
    (ts, kind, source, subject, lat, lon, value_num, value_text, payload, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class UnknownPayload(ValueError):
    """The payload matched no provider."""


def parse_payload(
    payload: Any, headers: dict[str, str], format_override: str | None = None
) -> tuple[Provider, ParseResult]:
    """Pick a provider and normalise the payload.

    A top-level list is treated as a batch of that provider's messages, which is
    how several recorders send more than one fix at a time.
    """
    probe = payload[0] if isinstance(payload, list) and payload else payload

    if format_override:
        provider = providers.get(format_override)
        if provider is None:
            raise UnknownPayload(f"Unknown format '{format_override}'")
    else:
        provider = providers.detect(probe)
        if provider is None:
            raise UnknownPayload("Payload did not match any known recorder format")

    if isinstance(payload, list):
        combined = ParseResult()
        for item in payload:
            part = provider.parse(item, headers)
            combined.points.extend(part.points)
            combined.events.extend(part.events)
        return provider, combined

    return provider, provider.parse(payload, headers)


def store(conn: sqlite3.Connection, parsed: ParseResult) -> IngestResult:
    """Persist normalised points and events, ignoring exact replays."""
    result = IngestResult()

    points = [p for p in parsed.points if p.ts > 0]
    if points:
        before = conn.total_changes
        conn.executemany(_INSERT_POINT, [_point_row(p) for p in points])
        result.accepted = conn.total_changes - before
        result.duplicates = len(points) - result.accepted

    if parsed.events:
        before = conn.total_changes
        conn.executemany(_INSERT_EVENT, [_event_row(e) for e in parsed.events])
        result.events = conn.total_changes - before

    touched = [p.ts for p in points] + [e.ts for e in parsed.events]
    if touched:
        mark_dirty(conn, min(touched))

    return result


def mark_dirty(conn: sqlite3.Connection, from_ts: int) -> None:
    """Record that derived rows from `from_ts` onward are stale.

    Only the earliest affected timestamp is kept — a rebuild always runs forward
    from there, so a later arrival never narrows the window.
    """
    current = db.get_state(conn, DIRTY_FROM_KEY)
    if current is None or from_ts < int(current):
        db.set_state(conn, DIRTY_FROM_KEY, str(from_ts))


def take_dirty_from(conn: sqlite3.Connection) -> int | None:
    """Read and clear the dirty marker."""
    current = db.get_state(conn, DIRTY_FROM_KEY)
    if current is None:
        return None
    conn.execute("DELETE FROM state WHERE key = ?", (DIRTY_FROM_KEY,))
    return int(current)


def _point_row(p: ParsedPoint) -> tuple:
    return (
        p.device,
        p.ts,
        p.lat,
        p.lon,
        p.accuracy,
        p.altitude,
        p.vertical_accuracy,
        p.speed_mps,
        p.heading,
        p.battery,
        p.battery_status,
        p.connection,
        p.trigger_type,
        p.ssid,
        p.bssid,
        p.pressure,
        p.monitoring_mode,
        p.in_regions,
        p.source,
        p.raw,
        int(time.time()),
    )


def _event_row(e: ParsedEvent) -> tuple:
    return (
        e.ts,
        e.kind,
        e.source,
        e.subject,
        e.lat,
        e.lon,
        e.value_num,
        e.value_text,
        e.payload,
        int(time.time()),
    )
