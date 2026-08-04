"""Quality flagging.

The load-bearing test here is the one asserting that a wide accuracy radius is
*not* grounds for dropping a fix. That is a deliberate reversal of the obvious
approach, and without a test pinning it someone will eventually "fix" it back.
"""

import pytest

from app import config, quality, segment
from app.quality import QualityPoint

from .conftest import BASE_TS, HOME, Track, offset_m


def qpoint(index: int, north_m: float = 0.0, accuracy: float = 10.0, step: int = 60):
    lat, lon = offset_m(HOME, north_m, 0.0)
    return QualityPoint(id=index, ts=BASE_TS + index * step, lat=lat, lon=lon, accuracy=accuracy)


# -- single-fix checks ------------------------------------------------------


def test_near_null_island_is_flagged_not_just_exact_zero():
    """Providers emit coordinates *near* (0,0) for "no fix", not exactly zero."""
    assert quality.flag_point(QualityPoint(1, BASE_TS, 0.0, 0.0, 5.0)) == quality.NULL_ISLAND
    assert quality.flag_point(QualityPoint(2, BASE_TS, 0.01, 0.01, 5.0)) == quality.NULL_ISLAND
    assert quality.flag_point(QualityPoint(3, BASE_TS, 1.0, 1.0, 5.0)) is None


def test_absurd_accuracy_is_flagged():
    assert (
        quality.flag_point(QualityPoint(1, BASE_TS, *HOME, 50_000.0))
        == quality.ABSURD_ACCURACY
    )


def test_a_wide_accuracy_radius_is_kept():
    """The decision this project turns on.

    A reported radius is a confidence estimate, not proof the position is wrong.
    Timeline exports routinely report kilometres of accuracy for fixes sitting
    exactly on the road; dropping them replaces real route geometry with straight
    lines. Accuracy weights the centroid instead of gating the point.
    """
    for radius in (100.0, 500.0, 1_000.0, 4_000.0):
        assert quality.flag_point(QualityPoint(1, BASE_TS, *HOME, radius)) is None


def test_missing_accuracy_is_not_grounds_for_rejection():
    assert quality.flag_point(QualityPoint(1, BASE_TS, *HOME, None)) is None


# -- the detour test --------------------------------------------------------


def test_an_out_and_back_excursion_is_flagged():
    """The characteristic stale cell or wifi fix.

    Each leg can look individually plausible, so a plain speed check misses it.
    The round trip cannot be explained.
    """
    points = [qpoint(0), qpoint(1), qpoint(2, north_m=80_000), qpoint(3), qpoint(4)]
    flagged = quality.find_outliers(points)
    assert set(flagged) == {2}
    assert flagged[2] == quality.DETOUR_SPEED


def test_genuine_one_way_travel_at_speed_is_not_flagged():
    """A plane covers ground fast but never doubles back.

    This is why the test measures the detour rather than raw speed: going
    straight at 250 m/s scores near zero here.
    """
    points = [qpoint(i, north_m=i * 250.0, step=1) for i in range(8)]
    assert quality.find_outliers(points) == {}


def test_a_run_of_consecutive_bad_fixes_is_flagged_together():
    points = [
        qpoint(0),
        qpoint(1),
        qpoint(2, north_m=60_000),
        qpoint(3, north_m=60_050),
        qpoint(4, north_m=60_100),
        qpoint(5),
        qpoint(6),
    ]
    assert set(quality.find_outliers(points)) == {2, 3, 4}


def test_runs_longer_than_the_limit_are_left_alone():
    """Beyond a handful of fixes it is more likely real travel than an artefact."""
    points = (
        [qpoint(0), qpoint(1)]
        + [qpoint(i, north_m=60_000 + i) for i in range(2, 12)]
        + [qpoint(12), qpoint(13)]
    )
    assert quality.find_outliers(points) == {}


def test_simultaneous_fixes_in_different_places_are_impossible():
    points = [
        qpoint(0),
        QualityPoint(1, BASE_TS, *offset_m(HOME, 5_000, 0), 10.0),
        QualityPoint(2, BASE_TS, *HOME, 10.0),
        qpoint(3),
    ]
    assert 1 in quality.find_outliers(points)


def test_a_clean_stream_flags_nothing():
    assert quality.find_outliers([qpoint(i, north_m=i * 20.0) for i in range(10)]) == {}


def test_too_few_points_to_judge_flags_nothing():
    assert quality.find_outliers([qpoint(0), qpoint(1)]) == {}


# -- persistence ------------------------------------------------------------


def test_flags_are_written_to_the_points_table(conn):
    (
        Track()
        .stay(minutes=20)
        .outlier(north_m=90_000)
        .stay(minutes=20)
        .insert(conn)
    )
    segment.rebuild(conn)

    row = conn.execute(
        "SELECT anomaly_reason FROM points WHERE anomaly = 1"
    ).fetchone()
    assert row["anomaly_reason"] == quality.DETOUR_SPEED


def test_flags_are_recomputed_rather_than_accumulated(conn):
    """A fix can stop looking like an outlier once its neighbours arrive.

    Flags are therefore cleared and recomputed wholesale on every rebuild.
    """
    Track().stay(minutes=20).outlier(north_m=90_000).stay(minutes=20).insert(conn)
    segment.rebuild(conn)
    first_pass = conn.execute("SELECT COUNT(*) AS n FROM points WHERE anomaly = 1").fetchone()["n"]

    segment.rebuild(conn)
    second_pass = conn.execute("SELECT COUNT(*) AS n FROM points WHERE anomaly = 1").fetchone()["n"]

    assert first_pass == second_pass == 1


def test_flagged_points_are_excluded_from_derived_geometry_but_kept(conn):
    Track().stay(minutes=20).outlier(north_m=90_000).stay(minutes=20).insert(conn)
    segment.rebuild(conn)

    total = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]
    flagged = conn.execute("SELECT COUNT(*) AS n FROM points WHERE anomaly = 1").fetchone()["n"]

    assert flagged == 1
    assert total > 1  # the raw fix is still there, merely marked

    stays = conn.execute("SELECT * FROM stays").fetchall()
    assert len(stays) == 1
    assert stays[0]["radius_m"] < 1_000


def test_thresholds_are_configurable(conn, monkeypatch):
    monkeypatch.setattr(config, "ABSURD_ACCURACY_M", 50.0)
    assert (
        quality.flag_point(QualityPoint(1, BASE_TS, *HOME, 100.0))
        == quality.ABSURD_ACCURACY
    )
