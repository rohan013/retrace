"""Deriving stays and trips from raw fixes.

The sweep follows trackintel's `sliding` staypoint algorithm, with two departures
that both come from how real phone data behaves rather than from theory.

**Drift cap.** Testing colocation against a running centroid alone lets a slow
walker drag the circle along indefinitely, so a leisurely walk collapses into one
fake "visit" sitting at the middle of the route. Every point must therefore fall
within the radius of the running centroid *and* within a hard multiple of it from
where the stay began.

**Gaps are decided by displacement, not by duration.** Both prior art choices are
wrong here. Splitting blindly on a long gap — trackintel's default, and
Dawarich's — misses the most common real stay on iOS: the phone goes quiet
precisely *because* it stopped moving, then reports again on the way out, and the
visit is never recorded. Never splitting invents journeys through unobserved
time. What matters is where the next fix lands: near the stay, and the silence
was the stay; far away, and the gap is genuinely unknown.

Derived rows are disposable. Any window can be deleted and rebuilt from `points`
alone, and nothing in `points` records which stay claimed it, so a stay can always
grow, shrink or be re-evaluated when new data arrives.
"""

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import config, db, quality
from .geo import distance_m, path_length_m, timezone_at

CURSOR_KEY = "segment_cursor_ts"

# A stay is never reported as tighter than this. GPS cannot support the claim,
# and a zero-radius circle renders as a dot the user cannot click.
MIN_STAY_RADIUS_M = 15.0

_CONFIDENCE_WEIGHTS = {
    "dwell": 0.30,
    "tightness": 0.25,
    "place_match": 0.20,
    "density": 0.15,
    "accuracy": 0.10,
}

# Multiplier applied when a stay spans a period with no fixes at all.
_GAP_CONFIDENCE_PENALTY = 0.85


@dataclass(slots=True)
class SegPoint:
    id: int
    device: str
    ts: int
    lat: float
    lon: float
    accuracy: float | None
    speed_mps: float | None


@dataclass(slots=True)
class Stay:
    device: str
    points: list[SegPoint]
    had_gap: bool = False
    _w_sum: float = 0.0
    _lat_w: float = 0.0
    _lon_w: float = 0.0

    def __post_init__(self) -> None:
        self._recompute()

    def _weight(self, point: SegPoint) -> float:
        return 1.0 / max(point.accuracy if point.accuracy is not None else 50.0, 1.0)

    def _recompute(self) -> None:
        self._w_sum = self._lat_w = self._lon_w = 0.0
        for point in self.points:
            self._accumulate(point)

    def _accumulate(self, point: SegPoint) -> None:
        w = self._weight(point)
        self._w_sum += w
        self._lat_w += point.lat * w
        self._lon_w += point.lon * w

    def add(self, point: SegPoint) -> None:
        self.points.append(point)
        self._accumulate(point)

    def absorb(self, other: "Stay") -> None:
        self.points.extend(other.points)
        self.had_gap = self.had_gap or other.had_gap
        self._recompute()

    @property
    def centroid(self) -> tuple[float, float]:
        return self._lat_w / self._w_sum, self._lon_w / self._w_sum

    @property
    def first(self) -> SegPoint:
        return self.points[0]

    @property
    def last(self) -> SegPoint:
        return self.points[-1]

    @property
    def duration(self) -> int:
        return self.last.ts - self.first.ts

    @property
    def radius_m(self) -> float:
        centre = self.centroid
        furthest = max(
            (distance_m(centre, (p.lat, p.lon)) for p in self.points), default=0.0
        )
        return max(furthest, MIN_STAY_RADIUS_M)

    @property
    def median_accuracy(self) -> float | None:
        values = [p.accuracy for p in self.points if p.accuracy is not None]
        return statistics.median(values) if values else None


@dataclass(slots=True)
class Trip:
    device: str
    points: list[SegPoint]
    from_stay_index: int | None = None
    to_stay_index: int | None = None

    @property
    def distance_m(self) -> float:
        return path_length_m([(p.lat, p.lon) for p in self.points])

    @property
    def duration(self) -> int:
        return self.points[-1].ts - self.points[0].ts


@dataclass
class RebuildResult:
    window_start: int
    stays: int = 0
    trips: int = 0
    flagged: int = 0
    devices: list[str] = field(default_factory=list)


# -- the sweep --------------------------------------------------------------


def detect_stays(points: Sequence[SegPoint]) -> list[Stay]:
    """Single pass over one device's fixes, in time order."""
    stays: list[Stay] = []
    open_stay: Stay | None = None

    for point in points:
        if open_stay is None:
            open_stay = Stay(device=point.device, points=[point])
            continue

        if point.ts - open_stay.last.ts > config.GAP_MAX_SECONDS:
            resumed = (
                distance_m(
                    (open_stay.last.lat, open_stay.last.lon), (point.lat, point.lon)
                )
                <= config.GAP_RESUME_DISTANCE_M
            )
            if resumed:
                open_stay.had_gap = True
                open_stay.add(point)
            else:
                _close(stays, open_stay)
                open_stay = Stay(device=point.device, points=[point])
            continue

        within_centroid = (
            distance_m(open_stay.centroid, (point.lat, point.lon)) <= config.STAY_RADIUS_M
        )
        within_drift_cap = (
            distance_m((open_stay.first.lat, open_stay.first.lon), (point.lat, point.lon))
            <= config.STAY_RADIUS_M * config.STAY_DRIFT_CAP
        )

        if within_centroid and within_drift_cap:
            open_stay.add(point)
        else:
            _close(stays, open_stay)
            open_stay = Stay(device=point.device, points=[point])

    _close(stays, open_stay)
    return merge_brief_reentries(stays)


def _close(stays: list[Stay], candidate: Stay | None) -> None:
    """Keep a candidate only if it dwelled long enough.

    Duration is the only gate. Point count deliberately is not one: with adaptive
    sampling a six-hour stay can produce two fixes, and rejecting it because it
    is sparse is how real visits get lost. Sparseness lowers confidence instead.
    """
    if candidate is not None and candidate.duration >= config.STAY_MIN_SECONDS:
        stays.append(candidate)


def merge_brief_reentries(stays: Sequence[Stay]) -> list[Stay]:
    """Rejoin stays split by a brief excursion — stepping outside and back."""
    merged: list[Stay] = []
    for stay in stays:
        if merged:
            previous = merged[-1]
            gap = stay.first.ts - previous.last.ts
            same_place = (
                distance_m(previous.centroid, stay.centroid) <= config.STAY_RADIUS_M
            )
            if gap <= config.MERGE_GAP_SECONDS and same_place:
                previous.absorb(stay)
                continue
        merged.append(stay)
    return merged


def detect_trips(points: Sequence[SegPoint], stays: Sequence[Stay]) -> list[Trip]:
    """Everything between stays.

    Each stay occupies a contiguous run of the device's fixes, so trips are just
    the index ranges left over. Slicing from a stay's last fix to the next stay's
    first fix also gives the geometry its endpoints for free — the drawn route
    reaches the places it connects instead of stopping short of them.
    """
    if len(points) < 2:
        return []

    index_of = {point.id: i for i, point in enumerate(points)}
    spans = [(index_of[stay.first.id], index_of[stay.last.id]) for stay in stays]

    if not spans:
        # Constant movement through the whole window, never settling anywhere.
        return [Trip(device=points[0].device, points=list(points))]

    trips: list[Trip] = []
    previous_end: int | None = None

    for stay_index, (start, end) in enumerate(spans):
        low = previous_end if previous_end is not None else 0
        segment = points[low : start + 1]
        if len(segment) >= 2:
            trips.append(
                Trip(
                    device=segment[0].device,
                    points=segment,
                    from_stay_index=stay_index - 1 if previous_end is not None else None,
                    to_stay_index=stay_index,
                )
            )
        previous_end = end

    if previous_end is not None and previous_end < len(points) - 1:
        segment = points[previous_end:]
        if len(segment) >= 2:
            trips.append(
                Trip(
                    device=segment[0].device,
                    points=segment,
                    from_stay_index=len(spans) - 1,
                    to_stay_index=None,
                )
            )

    return trips


# -- confidence -------------------------------------------------------------


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def confidence(stay: Stay, place_match: float | None = None) -> tuple[int, dict]:
    """A 0-100 score with its components kept.

    Storing the breakdown rather than just the number is what makes the algorithm
    debuggable from real data later: when a stay looks wrong, the reason it scored
    the way it did is already recorded.
    """
    components: dict[str, float] = {
        "dwell": _clamp(stay.duration / 1800),
        "tightness": _clamp(1 - stay.radius_m / config.STAY_RADIUS_M),
        "density": _clamp(len(stay.points) / 10),
    }

    median_accuracy = stay.median_accuracy
    if median_accuracy is not None:
        components["accuracy"] = _clamp(1 - (median_accuracy - 10) / 90)
    if place_match is not None:
        components["place_match"] = place_match

    total_weight = sum(_CONFIDENCE_WEIGHTS[k] for k in components)
    score = sum(components[k] * _CONFIDENCE_WEIGHTS[k] for k in components) / total_weight

    if stay.had_gap:
        score *= _GAP_CONFIDENCE_PENALTY

    return round(score * 100), components


# -- rebuild ----------------------------------------------------------------


def _load_points(conn: sqlite3.Connection, from_ts: int) -> list[SegPoint]:
    rows = conn.execute(
        """
        SELECT id, device, ts, lat, lon, accuracy, speed_mps
        FROM points
        WHERE ts >= ?
        ORDER BY device, ts
        """,
        (from_ts,),
    ).fetchall()
    return [SegPoint(**dict(row)) for row in rows]


def _window_start(conn: sqlite3.Connection, requested: int) -> int:
    """Widen the window so no derived row is half-rebuilt.

    A stay or trip that started before the requested point but ends inside it
    would otherwise be deleted and then rebuilt from only its tail. Reaching back
    to its true start keeps the rebuild whole.
    """
    row = conn.execute(
        """
        SELECT MIN(start_ts) AS earliest FROM (
            SELECT start_ts FROM stays WHERE end_ts >= :ts
            UNION ALL
            SELECT start_ts FROM trips WHERE end_ts >= :ts
        )
        """,
        {"ts": requested},
    ).fetchone()
    earliest = row["earliest"] if row else None
    return min(requested, earliest) if earliest is not None else requested


def rebuild(conn: sqlite3.Connection, from_ts: int | None = None) -> RebuildResult:
    """Delete and re-derive stays and trips from `from_ts` onward.

    Idempotent: running it twice over the same range produces the same rows.
    """
    requested = 0 if from_ts is None else max(0, from_ts - config.REBUILD_LOOKBACK_SECONDS)
    window_start = _window_start(conn, requested)

    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM trips WHERE end_ts >= ?", (window_start,))
        conn.execute("DELETE FROM stays WHERE end_ts >= ?", (window_start,))

        points = _load_points(conn, window_start)
        result = RebuildResult(window_start=window_start)

        flags = quality.apply(
            conn,
            [
                quality.QualityPoint(
                    id=p.id, ts=p.ts, lat=p.lat, lon=p.lon, accuracy=p.accuracy
                )
                for p in points
            ],
        )
        result.flagged = len(flags)

        trusted = [p for p in points if p.id not in flags]

        by_device: dict[str, list[SegPoint]] = {}
        for point in trusted:
            by_device.setdefault(point.device, []).append(point)

        for device, device_points in by_device.items():
            stays = detect_stays(device_points)
            trips = detect_trips(device_points, stays)
            stay_ids = _insert_stays(conn, stays)
            _insert_trips(conn, trips, stay_ids)
            result.stays += len(stays)
            result.trips += len(trips)
            result.devices.append(device)

        if points:
            db.set_state(conn, CURSOR_KEY, str(max(p.ts for p in points)))

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return result


def _insert_stays(conn: sqlite3.Connection, stays: Sequence[Stay]) -> list[int]:
    now = int(time.time())
    ids: list[int] = []
    for stay in stays:
        centre = stay.centroid
        score, breakdown = confidence(stay)
        cursor = conn.execute(
            """
            INSERT INTO stays
                (device, start_ts, end_ts, center_lat, center_lon, radius_m,
                 point_count, first_point_id, last_point_id, tz, had_gap,
                 confidence, confidence_breakdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stay.device,
                stay.first.ts,
                stay.last.ts,
                centre[0],
                centre[1],
                stay.radius_m,
                len(stay.points),
                stay.first.id,
                stay.last.id,
                timezone_at(centre[0], centre[1]),
                int(stay.had_gap),
                score,
                json.dumps(breakdown, sort_keys=True),
                now,
            ),
        )
        ids.append(int(cursor.lastrowid))
    return ids


def _insert_trips(
    conn: sqlite3.Connection, trips: Sequence[Trip], stay_ids: Sequence[int]
) -> None:
    now = int(time.time())
    for trip in trips:
        speeds = [p.speed_mps for p in trip.points if p.speed_mps is not None]
        duration = trip.duration
        distance = trip.distance_m

        def stay_id(index: int | None) -> int | None:
            if index is None or not (0 <= index < len(stay_ids)):
                return None
            return stay_ids[index]

        conn.execute(
            """
            INSERT INTO trips
                (device, start_ts, end_ts, distance_m, point_count,
                 start_lat, start_lon, end_lat, end_lon,
                 avg_speed, max_speed, from_stay_id, to_stay_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip.device,
                trip.points[0].ts,
                trip.points[-1].ts,
                distance,
                len(trip.points),
                trip.points[0].lat,
                trip.points[0].lon,
                trip.points[-1].lat,
                trip.points[-1].lon,
                distance / duration if duration > 0 else None,
                max(speeds) if speeds else None,
                stay_id(trip.from_stay_index),
                stay_id(trip.to_stay_index),
                now,
            ),
        )
