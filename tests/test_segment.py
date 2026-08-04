"""Segmentation behaviour.

Most cases here are drawn from failures in existing implementations: a long walk
collapsing into one phantom visit, a stay lost because the phone went quiet while
parked at it, a real visit rejected for being sparse. They are written as tests
so those behaviours cannot regress while thresholds get tuned.
"""

import pytest

from app import config, segment

from .conftest import BASE_TS, HOME, Track, offset_m

OFFICE = offset_m(HOME, 3_000, 2_000)
SHOP = offset_m(HOME, 400, 0)


def stays_and_trips(conn):
    stays = conn.execute("SELECT * FROM stays ORDER BY start_ts").fetchall()
    trips = conn.execute("SELECT * FROM trips ORDER BY start_ts").fetchall()
    return stays, trips


# -- the basics -------------------------------------------------------------


def test_a_simple_day_yields_two_stays_and_a_trip(conn):
    (
        Track()
        .stay(hours=2)
        .move_to(OFFICE, speed_mps=12, interval=30)
        .stay(hours=3)
        .insert(conn)
    )
    segment.rebuild(conn)

    stays, trips = stays_and_trips(conn)
    assert len(stays) == 2
    assert len(trips) == 1
    assert trips[0]["distance_m"] == pytest.approx(3_605, rel=0.05)


def test_sitting_still_all_day_is_one_stay_and_no_trips(conn):
    Track().stay(hours=8).insert(conn)
    segment.rebuild(conn)

    stays, trips = stays_and_trips(conn)
    assert len(stays) == 1
    assert stays[0]["point_count"] > 100
    assert trips == []


def test_a_brief_pause_is_not_a_stay(conn):
    """Under the dwell threshold, stopping at a light is just part of the trip."""
    (
        Track()
        .stay(hours=1)
        .move_to(SHOP, speed_mps=10, interval=30)
        .stay(minutes=2)
        .move_to(OFFICE, speed_mps=10, interval=30)
        .stay(hours=1)
        .insert(conn)
    )
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 2


# -- the cases prior implementations get wrong ------------------------------


def test_a_slow_walk_does_not_collapse_into_one_stay(conn):
    """A running centroid alone lets a walker drag the circle along.

    Without the drift cap this 500 m stroll registers as a single visit sitting
    in the middle of the route — a place the user never actually stopped.
    """
    destination = offset_m(HOME, 500, 0)
    Track().move_to(destination, speed_mps=1.2, interval=30).insert(conn)
    segment.rebuild(conn)

    stays, trips = stays_and_trips(conn)
    assert stays == []
    assert len(trips) == 1


def test_a_loop_walk_returning_to_its_start_is_not_a_stay(conn):
    """The centroid of a loop sits in the middle of it, where nobody stood."""
    track = Track()
    for corner in (
        offset_m(HOME, 300, 0),
        offset_m(HOME, 300, 300),
        offset_m(HOME, 0, 300),
        HOME,
    ):
        track.move_to(corner, speed_mps=1.4, interval=30)
    track.insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert stays == []


def test_circling_within_the_radius_is_bounded_by_the_drift_cap(conn):
    """The case the centroid check alone cannot catch.

    Pacing a circle keeps every fix inside the radius of the running centroid
    indefinitely, because the centroid sits at the centre of the circle. Only the
    cap on drift from the stay's first fix stops a 120 m-wide wander being
    reported as standing in one spot.
    """
    import math

    track = Track()
    radius_m = 60.0
    for step in range(40):
        angle = 2 * math.pi * step / 20
        track.pos = offset_m(
            HOME, radius_m * math.cos(angle), radius_m * math.sin(angle)
        )
        track.ts += 60
        track.point(accuracy=10.0)
    track.insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    limit = config.STAY_RADIUS_M * config.STAY_DRIFT_CAP
    for stay in stays:
        first = conn.execute(
            "SELECT lat, lon FROM points WHERE id = ?", (stay["first_point_id"],)
        ).fetchone()
        last = conn.execute(
            "SELECT lat, lon FROM points WHERE id = ?", (stay["last_point_id"],)
        ).fetchone()
        from app.geo import distance_m

        assert distance_m((first["lat"], first["lon"]), (last["lat"], last["lon"])) <= limit


def test_a_gap_while_stationary_stays_one_visit(conn):
    """The phone going quiet is the strongest stay signal iOS produces.

    It stops reporting *because* it stopped moving. Splitting here is how a visit
    to the gym gets lost: tracking stops on arrival and resumes on the way out.
    """
    Track().stay(minutes=20).gap(hours=2).stay(minutes=20).insert(conn)
    segment.rebuild(conn)

    stays, trips = stays_and_trips(conn)
    assert len(stays) == 1
    assert stays[0]["had_gap"] == 1
    assert stays[0]["end_ts"] - stays[0]["start_ts"] > 2 * 3600
    assert trips == []


def test_days_of_silence_do_not_resume_a_stay(conn):
    """Displacement decides, but only within reason.

    Two visits to the same place with the phone off in between must not become
    one multi-day stay. Nobody stood still for 55 hours, and the resume rule has
    to stop somewhere short of claiming they did.
    """
    Track().stay(hours=2).insert(conn)
    Track(start_ts=BASE_TS + 200_000).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 2


def test_an_overnight_gap_still_resumes(conn):
    """The bound must not be so tight it breaks the case it exists to serve."""
    Track().stay(minutes=30).gap(hours=9).stay(minutes=30).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 1
    assert stays[0]["had_gap"] == 1


def test_a_gap_while_travelling_splits(conn):
    """Silence plus displacement means the time is genuinely unaccounted for."""
    track = Track().stay(minutes=20).gap(hours=2)
    track.pos = OFFICE
    track.stay(minutes=20).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 2


def test_a_gapped_stay_scores_lower_than_a_continuous_one(conn):
    Track(device="gapped").stay(minutes=20).gap(hours=2).stay(minutes=20).insert(conn)
    Track(device="continuous").stay(hours=2, interval=60).insert(conn)
    segment.rebuild(conn)

    gapped = conn.execute("SELECT * FROM stays WHERE device = 'gapped'").fetchone()
    continuous = conn.execute("SELECT * FROM stays WHERE device = 'continuous'").fetchone()
    assert gapped["confidence"] < continuous["confidence"]


def test_a_two_point_stay_is_still_detected(conn):
    """Adaptive sampling can report a six-hour stay in two fixes.

    Rejecting it for being sparse is how real visits disappear — a weekend spent
    largely sitting still generates almost no points. Point count lowers
    confidence rather than acting as a gate.
    """
    Track().stay(hours=6, interval=6 * 3600).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 1
    assert stays[0]["point_count"] == 2
    assert stays[0]["end_ts"] - stays[0]["start_ts"] == 6 * 3600
    # Sparseness is priced in rather than disqualifying.
    assert stays[0]["confidence"] < 70


def test_a_teleport_outlier_is_flagged_without_losing_the_route(conn):
    """A stale fix jumping away and back must not truncate the real geometry."""
    (
        Track()
        .stay(minutes=30)
        .move_to(OFFICE, speed_mps=12, interval=30)
        .outlier(north_m=80_000)
        .move_to(offset_m(OFFICE, 500, 500), speed_mps=12, interval=30)
        .stay(minutes=30)
        .insert(conn)
    )
    segment.rebuild(conn)

    flagged = conn.execute(
        "SELECT COUNT(*) AS n FROM points WHERE anomaly = 1 AND anomaly_reason = 'detour_speed'"
    ).fetchone()["n"]
    assert flagged == 1

    stays, trips = stays_and_trips(conn)
    assert len(stays) == 2
    # The route survives: no trip stretches 80 km out and back.
    assert all(trip["distance_m"] < 20_000 for trip in trips)


def test_stepping_outside_and_back_is_one_merged_stay(conn):
    track = Track().stay(minutes=30)
    track.move_to(offset_m(HOME, 250, 0), speed_mps=1.4, interval=30)
    track.move_to(HOME, speed_mps=1.4, interval=30)
    track.stay(minutes=30).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 1


def test_two_devices_are_segmented_independently(conn):
    """A phone left at home must never be stitched to one that travelled."""
    Track(device="phone").stay(hours=2).move_to(OFFICE, speed_mps=12).stay(
        hours=2
    ).insert(conn)
    Track(device="tablet").stay(hours=6).insert(conn)
    segment.rebuild(conn)

    phone = conn.execute("SELECT * FROM stays WHERE device = 'phone'").fetchall()
    tablet = conn.execute("SELECT * FROM stays WHERE device = 'tablet'").fetchall()
    assert len(phone) == 2
    assert len(tablet) == 1

    trips = conn.execute("SELECT DISTINCT device FROM trips").fetchall()
    assert [row["device"] for row in trips] == ["phone"]


def test_a_stay_spanning_midnight_is_one_stay(conn):
    """Segmentation is unaware of calendar days; only presentation splits them."""
    midnight = BASE_TS - (BASE_TS % 86400)
    Track(start_ts=midnight - 3600).stay(hours=2, interval=60).insert(conn)
    segment.rebuild(conn)

    stays, _ = stays_and_trips(conn)
    assert len(stays) == 1
    assert stays[0]["start_ts"] < midnight < stays[0]["end_ts"]


# -- stored geometry --------------------------------------------------------


def test_stay_geometry_is_stored_not_recomputed(conn):
    Track().stay(hours=2, jitter_m=20).insert(conn)
    segment.rebuild(conn)

    stay = conn.execute("SELECT * FROM stays").fetchone()
    assert stay["center_lat"] == pytest.approx(HOME[0], abs=1e-3)
    assert stay["center_lon"] == pytest.approx(HOME[1], abs=1e-3)
    assert stay["radius_m"] >= segment.MIN_STAY_RADIUS_M
    assert stay["first_point_id"] is not None
    assert stay["last_point_id"] is not None


def test_a_stay_is_never_reported_tighter_than_gps_can_support(conn):
    Track().stay(hours=1, jitter_m=0.0).insert(conn)
    segment.rebuild(conn)
    assert conn.execute("SELECT radius_m FROM stays").fetchone()["radius_m"] == (
        segment.MIN_STAY_RADIUS_M
    )


def test_stays_are_stamped_with_a_timezone_from_their_own_coordinates(conn):
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)
    assert conn.execute("SELECT tz FROM stays").fetchone()["tz"] == "Europe/London"


def test_confidence_breakdown_is_persisted(conn):
    import json

    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    stay = conn.execute("SELECT * FROM stays").fetchone()
    breakdown = json.loads(stay["confidence_breakdown"])
    # place_match is absent because nothing has been named yet — an unmatched
    # stay omits the component rather than being penalised for it.
    assert set(breakdown) == {"dwell", "tightness", "density", "accuracy"}
    assert 0 <= stay["confidence"] <= 100


def test_trips_reference_the_stays_they_connect(conn):
    Track().stay(hours=1).move_to(OFFICE, speed_mps=12).stay(hours=1).insert(conn)
    segment.rebuild(conn)

    trip = conn.execute("SELECT * FROM trips").fetchone()
    stays, _ = stays_and_trips(conn)
    assert trip["from_stay_id"] == stays[0]["id"]
    assert trip["to_stay_id"] == stays[1]["id"]


# -- thresholds are configurable -------------------------------------------


def test_lowering_the_dwell_threshold_surfaces_shorter_stays(conn, monkeypatch):
    Track().stay(hours=1).move_to(SHOP, speed_mps=1.4).stay(minutes=3).move_to(
        OFFICE, speed_mps=12
    ).stay(hours=1).insert(conn)

    segment.rebuild(conn)
    assert len(conn.execute("SELECT * FROM stays").fetchall()) == 2

    monkeypatch.setattr(config, "STAY_MIN_SECONDS", 120)
    segment.rebuild(conn)
    assert len(conn.execute("SELECT * FROM stays").fetchall()) == 3
