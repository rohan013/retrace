"""Marking untrustworthy fixes.

The important decision here is what this module does *not* do: it does not drop
points for having a large reported accuracy radius.

That radius is a confidence estimate, not proof the position is wrong. Phone and
Timeline exports routinely report 1-4 km accuracy for fixes sitting exactly on
the road. Filtering those out removes real route geometry and replaces it with
straight lines between whatever survived — the map gets tidier and less true.
Accuracy is therefore used as a *weight* when computing stay centres, and only
absurd values are flagged.

The filter that actually earns its place is the detour test, which catches the
characteristic phone failure: a stale cell or wifi fix that teleports far away
and back again between two perfectly sane neighbours. Each leg of that round trip
can look individually plausible, so speed alone misses it; the round trip cannot.
"""

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from . import config
from .geo import distance_m

NULL_ISLAND = "null_island"
ABSURD_ACCURACY = "absurd_accuracy"
DETOUR_SPEED = "detour_speed"


@dataclass(slots=True)
class QualityPoint:
    """The subset of a point row the quality checks need."""

    id: int
    ts: int
    lat: float
    lon: float
    accuracy: float | None


def flag_point(point: QualityPoint) -> str | None:
    """Reasons that depend on a single fix in isolation."""
    if distance_m((point.lat, point.lon), (0.0, 0.0)) <= config.NULL_ISLAND_RADIUS_M:
        # Providers emit coordinates *near* (0,0) for "no fix", not exactly zero,
        # so a radius is needed rather than an equality check.
        return NULL_ISLAND
    if point.accuracy is not None and point.accuracy > config.ABSURD_ACCURACY_M:
        return ABSURD_ACCURACY
    return None


def _detour_speed(
    prev: QualityPoint, run: Sequence[QualityPoint], nxt: QualityPoint
) -> float:
    """Excess speed implied by detouring through `run` instead of going direct.

    A genuine one-way journey scores near zero: the detour distance collapses to
    roughly the direct distance. An out-and-back excursion scores double, because
    both legs are added while the direct distance stays small.
    """
    detour = (
        distance_m((prev.lat, prev.lon), (run[0].lat, run[0].lon))
        + distance_m((run[-1].lat, run[-1].lon), (nxt.lat, nxt.lon))
        - distance_m((prev.lat, prev.lon), (nxt.lat, nxt.lon))
    )
    elapsed = nxt.ts - prev.ts
    if elapsed <= 0:
        # Simultaneous fixes in different places: impossible unless they coincide.
        return float("inf") if detour > 1.0 else 0.0
    return detour / elapsed


def find_outliers(points: Sequence[QualityPoint]) -> dict[int, str]:
    """Flag runs of fixes that could only be reached by teleporting.

    Runs are tried shortest first so a single bad fix is caught on its own rather
    than dragging its innocent neighbours into a longer run.
    """
    flagged: dict[int, str] = {}

    for run_len in range(1, config.MAX_OUTLIER_RUN + 1):
        clean = [p for p in points if p.id not in flagged]
        if len(clean) < run_len + 2:
            break

        i = 1
        while i + run_len < len(clean):
            run = clean[i : i + run_len]
            if _detour_speed(clean[i - 1], run, clean[i + run_len]) > config.MAX_DETOUR_SPEED_MPS:
                for p in run:
                    flagged[p.id] = DETOUR_SPEED
                i += run_len
            else:
                i += 1

    return flagged


def apply(conn: sqlite3.Connection, points: Sequence[QualityPoint]) -> dict[int, str]:
    """Recompute quality flags for a set of points and persist them.

    Flags are derived, so they are recomputed wholesale rather than patched —
    a point that looked like an outlier can stop looking like one once its
    neighbours arrive.
    """
    reasons: dict[int, str] = {}

    survivors = []
    for point in points:
        reason = flag_point(point)
        if reason:
            reasons[point.id] = reason
        else:
            survivors.append(point)

    reasons.update(find_outliers(survivors))

    if points:
        conn.executemany(
            "UPDATE points SET anomaly = 0, anomaly_reason = NULL WHERE id = ?",
            [(p.id,) for p in points],
        )
    if reasons:
        conn.executemany(
            "UPDATE points SET anomaly = 1, anomaly_reason = ? WHERE id = ?",
            [(reason, pid) for pid, reason in reasons.items()],
        )

    return reasons
