"""Place and area resolution.

Two behaviours matter most: a user-chosen name must never be overwritten by a
later automatic pass, and an unmatched stay must not spawn a placeholder place.
Both are failure modes that only become visible after months of accumulated data.
"""

import time

import pytest

from app import config, places, segment

from .conftest import HOME, Track, offset_m

OFFICE = offset_m(HOME, 3_000, 2_000)


def make_area(conn, name, centre, radius_m):
    cursor = conn.execute(
        "INSERT INTO areas (name, lat, lon, radius_m, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, centre[0], centre[1], radius_m, int(time.time())),
    )
    return int(cursor.lastrowid)


# -- areas ------------------------------------------------------------------


def test_a_stay_inside_a_user_drawn_area_is_matched_to_it(conn):
    area_id = make_area(conn, "Home", HOME, 150)
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    stay = conn.execute("SELECT * FROM stays").fetchone()
    assert stay["area_id"] == area_id
    assert stay["place_id"] is None


def test_the_smallest_containing_area_wins(conn):
    """A specific area nested in a broader one should take precedence."""
    make_area(conn, "Campus", HOME, 2_000)
    desk = make_area(conn, "Desk", HOME, 50)

    match = places.find_area(conn, *HOME)
    assert match["id"] == desk


def test_a_stay_outside_every_area_matches_none(conn):
    make_area(conn, "Home", HOME, 100)
    assert places.find_area(conn, *OFFICE) is None


def test_an_area_match_scores_higher_than_a_place_match(conn):
    """A hand-drawn boundary is better evidence than a geocoded guess."""
    assert places.AREA_MATCH_SCORE > places.PLACE_MATCH_SCORE > places.NO_MATCH_SCORE


def test_an_area_match_raises_confidence(conn):
    Track(device="unnamed").stay(hours=2).insert(conn)
    segment.rebuild(conn)
    without = conn.execute("SELECT confidence FROM stays").fetchone()["confidence"]

    make_area(conn, "Home", HOME, 150)
    segment.rebuild(conn)
    with_area = conn.execute("SELECT confidence FROM stays").fetchone()["confidence"]

    assert with_area > without


# -- places -----------------------------------------------------------------


def test_an_unmatched_stay_does_not_mint_a_placeholder_place(conn):
    """Auto-creating a place per unmatched stay is how the table reaches
    thousands of rows called "Suggested place"."""
    Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM places").fetchone()["n"] == 0
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM stays WHERE place_id IS NULL").fetchone()["n"]
        == 2
    )


def test_a_nearby_existing_place_is_reused(conn):
    place_id = places.create_place(conn, "Cafe", *offset_m(HOME, 20, 0))
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT place_id FROM stays").fetchone()["place_id"] == place_id


def test_a_distant_place_is_not_reused(conn):
    places.create_place(conn, "Cafe", *offset_m(HOME, 5_000, 0))
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT place_id FROM stays").fetchone()["place_id"] is None


def test_the_nearest_of_several_candidate_places_wins(conn):
    places.create_place(conn, "Far", *offset_m(HOME, 45, 0))
    near = places.create_place(conn, "Near", *offset_m(HOME, 5, 0))
    assert places.find_place(conn, *HOME)["id"] == near


def test_naming_a_place_attaches_past_visits(conn):
    """Naming somewhere once should name every visit to it, past and future."""
    Track().stay(hours=2).insert(conn)
    Track(start_ts=1780304400 + 200_000).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 2

    place_id = places.create_place(conn, "Home", *HOME)
    attached = places.attach_nearby_stays(conn, place_id)

    assert attached == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM stays WHERE place_id = ?", (place_id,)
        ).fetchone()["n"]
        == 2
    )


def test_attaching_ignores_stays_already_matched_to_an_area(conn):
    make_area(conn, "Home", HOME, 150)
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    place_id = places.create_place(conn, "Somewhere", *HOME)
    assert places.attach_nearby_stays(conn, place_id) == 0


def test_attaching_to_a_missing_place_is_harmless(conn):
    assert places.attach_nearby_stays(conn, 9999) == 0


# -- name locking -----------------------------------------------------------


def test_a_manually_named_place_is_locked(conn):
    place_id = places.create_place(conn, "Home", *HOME)
    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    assert row["name_locked_at"] is not None
    assert row["source"] == "manual"


def test_renaming_locks_and_survives_rebuilds(conn):
    place_id = places.create_place(conn, "Guess", *HOME, source="auto", lock_name=False)
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    places.rename_place(conn, place_id, "The Good Cafe")
    segment.rebuild(conn)
    segment.rebuild(conn)

    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    assert row["name"] == "The Good Cafe"
    assert row["name_locked_at"] is not None
    assert row["source"] == "manual"


# -- OSM name cleaning ------------------------------------------------------


@pytest.mark.parametrize("value", ["yes", "no", "YES", " ", "", None, 42])
def test_generic_osm_values_are_rejected_as_names(value):
    """`building=yes` surfacing as a place called "Yes" is a real failure."""
    assert places.clean_name(value) is None


def test_a_real_name_survives_cleaning():
    assert places.clean_name("  The Ivy  ") == "The Ivy"


def test_name_preference_runs_from_specific_to_generic():
    assert places.name_from_osm({"name": "Ivy", "address": {"road": "High St"}}) == "Ivy"
    assert places.name_from_osm({"address": {"amenity": "Library"}}) == "Library"
    assert (
        places.name_from_osm({"address": {"house_number": "12", "road": "High St"}})
        == "12 High St"
    )
    assert places.name_from_osm({"address": {"road": "High St"}}) == "High St"


def test_a_generic_building_tag_does_not_become_the_name():
    raw = {"address": {"building": "yes", "road": "High St"}}
    assert places.name_from_osm(raw) == "High St"


def test_display_name_is_the_last_resort():
    assert places.name_from_osm({"display_name": "Somewhere, London, UK"}) == "Somewhere"


def test_no_usable_name_returns_none():
    assert places.name_from_osm({}) is None
    assert places.name_from_osm({"address": {"building": "yes"}}) is None


# -- geocoding is opt-in ----------------------------------------------------


def test_geocoding_is_disabled_by_default(conn):
    """A rebuild must never issue a network request per stay."""
    assert config.GEOCODING_ENABLED is False
    assert places.reverse_geocode(*HOME) is None


def test_a_rebuild_makes_no_geocoding_calls(conn, monkeypatch):
    monkeypatch.setattr(config, "GEOCODING_ENABLED", True)

    calls = []
    monkeypatch.setattr(
        places, "reverse_geocode", lambda lat, lon: calls.append((lat, lon))
    )

    Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert calls == []
