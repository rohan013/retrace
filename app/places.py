"""Naming the locations a stay happened at.

Resolution runs cheapest-first, and the ordering is the whole design:

1. **User-drawn areas.** Ten boxes — home, work, gym, parents — name the large
   majority of anyone's stay-time with no network call and no ambiguity. They are
   checked first because a hand-drawn boundary is better evidence than anything a
   geocoder can infer.
2. **An existing place nearby.** Reuse rather than mint a near-duplicate.
3. **Nothing.** `place_id` stays null.

Step 3 is deliberate. Auto-creating a placeholder place per unmatched stay is how
a places table grows to thousands of rows named "Suggested place", which then
makes every subsequent lookup slower and every list unusable. A stay with no
place is simply an unnamed stay, and naming it is a user action.

Reverse geocoding is never called during a rebuild. A rebuild processes the whole
history and would issue one network request per stay; instead geocoding is an
explicit, opt-in, per-stay action.
"""

import sqlite3
import time
from typing import Any

from . import config
from .geo import distance_m

# Values OSM returns that are categories rather than names. `building=yes` leaking
# through as a place called "Yes" is a real failure mode.
_GENERIC_NAMES = {"yes", "no", "true", "false", "unclassified", "unknown"}

_NAME_KEYS = ("amenity", "shop", "office", "leisure", "tourism", "building", "club")

AREA_MATCH_SCORE = 1.0
PLACE_MATCH_SCORE = 0.7
NO_MATCH_SCORE = 0.0


def find_area(conn: sqlite3.Connection, lat: float, lon: float) -> sqlite3.Row | None:
    """The user-drawn box containing this point, smallest first.

    Smallest wins so a specific area nested inside a broader one — a desk inside
    an office — takes precedence. Ranking by degree-area rather than square
    metres is exact here: every candidate contains the same query point, so they
    sit at the same latitude, and degree-area orders them identically.
    """
    best = None
    best_size = float("inf")
    for area in conn.execute("SELECT * FROM areas"):
        if area["min_lat"] <= lat <= area["max_lat"] and area["min_lon"] <= lon <= area["max_lon"]:
            size = (area["max_lat"] - area["min_lat"]) * (area["max_lon"] - area["min_lon"])
            if size < best_size:
                best, best_size = area, size
    return best


def find_place(
    conn: sqlite3.Connection, lat: float, lon: float, radius_m: float | None = None
) -> sqlite3.Row | None:
    """The nearest existing place within the reuse radius."""
    limit = radius_m if radius_m is not None else config.PLACE_REUSE_RADIUS_M
    best = None
    best_distance = float("inf")
    for place in conn.execute("SELECT * FROM places"):
        d = distance_m((lat, lon), (place["lat"], place["lon"]))
        if d <= limit and d < best_distance:
            best, best_distance = place, d
    return best


def resolve(
    conn: sqlite3.Connection, lat: float, lon: float
) -> tuple[int | None, int | None, float]:
    """Match a stay centroid to an area or place.

    Returns (area_id, place_id, match_score). The score feeds the stay's
    confidence: a hand-drawn area is stronger evidence than a geocoded place.
    """
    area = find_area(conn, lat, lon)
    if area is not None:
        return area["id"], None, AREA_MATCH_SCORE

    place = find_place(conn, lat, lon)
    if place is not None:
        return None, place["id"], PLACE_MATCH_SCORE

    return None, None, NO_MATCH_SCORE


def create_place(
    conn: sqlite3.Connection,
    name: str,
    lat: float,
    lon: float,
    *,
    radius_m: float | None = None,
    address: str | None = None,
    category: str | None = None,
    source: str = "manual",
    lock_name: bool = True,
) -> int:
    """Create a place. Naming it by hand locks the name against later guesses."""
    now = int(time.time())
    cursor = conn.execute(
        """
        INSERT INTO places
            (name, lat, lon, radius_m, address, category, source,
             name_locked_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            lat,
            lon,
            radius_m,
            address,
            category,
            source,
            now if lock_name else None,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def rename_place(conn: sqlite3.Connection, place_id: int, name: str) -> None:
    """Rename and lock.

    The lock is what stops a later automatic pass overwriting a name the user
    chose — the single most common complaint against tools in this space.
    """
    now = int(time.time())
    conn.execute(
        "UPDATE places SET name = ?, name_locked_at = ?, source = 'manual', updated_at = ? "
        "WHERE id = ?",
        (name, now, now, place_id),
    )


def attach_nearby_stays(conn: sqlite3.Connection, place_id: int) -> int:
    """Link every unassigned stay near a place to it.

    Naming somewhere once should name every past and future visit to it.
    """
    place = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        return 0

    attached = 0
    for stay in conn.execute(
        "SELECT id, center_lat, center_lon FROM stays WHERE place_id IS NULL AND area_id IS NULL"
    ).fetchall():
        distance = distance_m(
            (stay["center_lat"], stay["center_lon"]), (place["lat"], place["lon"])
        )
        if distance <= config.PLACE_REUSE_RADIUS_M:
            conn.execute("UPDATE stays SET place_id = ? WHERE id = ?", (place_id, stay["id"]))
            attached += 1
    return attached


# -- reverse geocoding ------------------------------------------------------


def clean_name(candidate: Any) -> str | None:
    """Reject OSM values that are categories masquerading as names."""
    if not isinstance(candidate, str):
        return None
    stripped = candidate.strip()
    if not stripped or stripped.lower() in _GENERIC_NAMES:
        return None
    return stripped


def name_from_osm(raw: dict) -> str | None:
    """Best available name from a Nominatim response.

    Preference runs from an actual name, through a categorised feature, down to
    a street address — never a bare category.
    """
    direct = clean_name(raw.get("name"))
    if direct:
        return direct

    address = raw.get("address") or {}
    for key in _NAME_KEYS:
        named = clean_name(address.get(key))
        if named:
            return named

    road = clean_name(address.get("road"))
    if road:
        number = clean_name(address.get("house_number"))
        return f"{number} {road}" if number else road

    display = clean_name(raw.get("display_name"))
    if display:
        return display.split(",")[0].strip() or None
    return None


def reverse_geocode(lat: float, lon: float) -> dict | None:
    """Look up a single coordinate.

    Called only on an explicit request for one stay, never in a rebuild loop.
    geopy applies no throttling of its own — the RateLimiter here is opt-in and
    omitting it is how a personal project gets blocked by Nominatim.
    """
    if not config.GEOCODING_ENABLED:
        return None

    from geopy.extra.rate_limiter import RateLimiter
    from geopy.geocoders import Nominatim

    geocoder = Nominatim(user_agent=config.GEOCODER_USER_AGENT, timeout=10)
    reverse = RateLimiter(geocoder.reverse, min_delay_seconds=1.0, max_retries=2)

    location = reverse((lat, lon), exactly_one=True, addressdetails=True)
    if location is None:
        return None

    raw = location.raw
    return {
        "name": name_from_osm(raw),
        "address": raw.get("display_name"),
        "category": raw.get("category") or raw.get("type"),
        "raw": raw,
    }
