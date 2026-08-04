"""Geometry helpers.

Distances use haversine on a spherical earth rather than geopy's exact geodesic.
At the ~70 m scale the segmentation thresholds care about the two disagree by
0.08 % — 6 cm, far below GPS noise of +/-5-50 m — but haversine measured 65x
faster here. That matters because rebuilding a year of history costs ~4 s instead
of ~4.5 min, and a full rebuild happens every time a threshold is tuned.

geopy is still a dependency: it does the reverse geocoding in places.py, and
`geodesic_m` below is kept so tests can assert the approximation stays honest.
"""

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in metres between (lat, lon) pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# The pipeline calls this name; swapping the implementation is a one-line change.
distance_m = haversine_m


def geodesic_m(a: LatLon, b: LatLon) -> float:
    """Exact WGS84 distance. Slow; used for verification, not in the pipeline."""
    from geopy.distance import geodesic

    return geodesic(a, b).meters


def weighted_centroid(
    coords: Sequence[LatLon], accuracies: Sequence[float | None] | None = None
) -> LatLon:
    """Accuracy-weighted mean position.

    A fix reporting 5 m accuracy should pull the centre harder than one
    reporting 200 m, so weight is 1/accuracy. Missing accuracy is treated as
    50 m — pessimistic enough not to dominate, optimistic enough to count.
    """
    if not coords:
        raise ValueError("weighted_centroid requires at least one coordinate")
    if accuracies is None:
        accuracies = [None] * len(coords)

    total = 0.0
    lat_sum = 0.0
    lon_sum = 0.0
    for (lat, lon), acc in zip(coords, accuracies):
        w = 1.0 / max(acc if acc is not None else 50.0, 1.0)
        lat_sum += lat * w
        lon_sum += lon * w
        total += w
    return lat_sum / total, lon_sum / total


def max_distance_from(centre: LatLon, coords: Iterable[LatLon]) -> float:
    """Radius of a point cloud: the furthest member from its centre."""
    return max((distance_m(centre, c) for c in coords), default=0.0)


def bbox(coords: Sequence[LatLon]) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon)."""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def _perpendicular_distance(p: LatLon, start: LatLon, end: LatLon) -> float:
    """Distance from p to the segment start-end, in metres.

    Works in a local flat projection, which is fine over the short spans this is
    used on and avoids the cost of a proper projection.
    """
    if start == end:
        return distance_m(p, start)

    lat_ref = math.radians((start[0] + end[0]) / 2)
    mx = EARTH_RADIUS_M * math.cos(lat_ref)

    def xy(c: LatLon) -> tuple[float, float]:
        return math.radians(c[1]) * mx, math.radians(c[0]) * EARTH_RADIUS_M

    px, py = xy(p)
    ax, ay = xy(start)
    bx, by = xy(end)

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def douglas_peucker(coords: Sequence[LatLon], tolerance_m: float) -> list[LatLon]:
    """Simplify a polyline, keeping points that deviate more than tolerance_m.

    Used to shrink multi-day tracks before sending them to the browser. A single
    day is small enough to ship whole.
    """
    if len(coords) < 3:
        return list(coords)

    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        furthest_i = -1
        furthest_d = 0.0
        for i in range(first + 1, last):
            d = _perpendicular_distance(coords[i], coords[first], coords[last])
            if d > furthest_d:
                furthest_i, furthest_d = i, d
        if furthest_d > tolerance_m:
            keep[furthest_i] = True
            stack.append((first, furthest_i))
            stack.append((furthest_i, last))

    return [c for c, k in zip(coords, keep) if k]


def path_length_m(coords: Sequence[LatLon]) -> float:
    """Total length along a polyline."""
    return sum(distance_m(coords[i - 1], coords[i]) for i in range(1, len(coords)))


def timezone_at(lat: float, lon: float) -> str | None:
    """IANA timezone for a coordinate.

    Wraps tzfpy, whose signature is get_tz(lon, lat) — longitude first. Passing
    them the wrong way round returns a plausible-looking wrong answer rather than
    raising, so this wrapper exists to make that mistake impossible.
    """
    from tzfpy import get_tz

    return get_tz(lon, lat)
