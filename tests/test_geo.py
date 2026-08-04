import math

import pytest

from app import geo

LONDON = (51.5074, -0.1278)


def test_haversine_matches_geodesic_at_segmentation_scale():
    """The pipeline swaps exact geodesic for haversine on a speed argument.

    That trade is only defensible while the two agree at the scale the stay
    thresholds operate on, so pin it: under 0.2 % out to a few hundred metres.
    """
    for offset in (0.0001, 0.0005, 0.001, 0.005):
        b = (LONDON[0] + offset, LONDON[1] + offset)
        fast = geo.haversine_m(LONDON, b)
        exact = geo.geodesic_m(LONDON, b)
        assert abs(fast - exact) / exact < 0.002


def test_haversine_is_symmetric_and_zero_for_identical_points():
    a, b = LONDON, (51.52, -0.10)
    assert geo.haversine_m(a, b) == pytest.approx(geo.haversine_m(b, a))
    assert geo.haversine_m(a, a) == 0.0


def test_weighted_centroid_is_pulled_toward_the_accurate_fix():
    """A 5 m fix should outvote a 200 m fix rather than being averaged with it."""
    coords = [(51.0, 0.0), (51.001, 0.0)]
    centre = geo.weighted_centroid(coords, accuracies=[5.0, 200.0])
    assert centre[0] < 51.0002

    unweighted = geo.weighted_centroid(coords, accuracies=[10.0, 10.0])
    assert unweighted[0] == pytest.approx(51.0005)


def test_weighted_centroid_treats_missing_accuracy_as_fifty_metres():
    coords = [(51.0, 0.0), (51.001, 0.0)]
    assert geo.weighted_centroid(coords, [None, None]) == pytest.approx(
        geo.weighted_centroid(coords, [50.0, 50.0])
    )


def test_weighted_centroid_rejects_empty_input():
    with pytest.raises(ValueError):
        geo.weighted_centroid([])


def test_max_distance_from_reports_the_furthest_member():
    centre = (51.0, 0.0)
    coords = [(51.0, 0.0), (51.0005, 0.0), (51.002, 0.0)]
    assert geo.max_distance_from(centre, coords) == pytest.approx(
        geo.haversine_m(centre, (51.002, 0.0))
    )
    assert geo.max_distance_from(centre, []) == 0.0


def test_douglas_peucker_drops_collinear_points_but_keeps_the_shape():
    straight = [(51.0 + i * 0.001, 0.0) for i in range(10)]
    assert geo.douglas_peucker(straight, 10.0) == [straight[0], straight[-1]]

    with_detour = straight[:5] + [(51.0045, 0.002)] + straight[5:]
    simplified = geo.douglas_peucker(with_detour, 10.0)
    assert (51.0045, 0.002) in simplified
    assert simplified[0] == with_detour[0]
    assert simplified[-1] == with_detour[-1]


def test_douglas_peucker_passes_short_paths_through():
    assert geo.douglas_peucker([], 5.0) == []
    assert geo.douglas_peucker([LONDON], 5.0) == [LONDON]
    assert geo.douglas_peucker([LONDON, LONDON], 5.0) == [LONDON, LONDON]


def test_path_length_sums_segments():
    coords = [(51.0, 0.0), (51.001, 0.0), (51.002, 0.0)]
    assert geo.path_length_m(coords) == pytest.approx(
        geo.haversine_m(coords[0], coords[1]) * 2, rel=1e-6
    )
    assert geo.path_length_m([LONDON]) == 0.0


def test_timezone_at_takes_lat_lon_in_that_order():
    """tzfpy's own signature is get_tz(lon, lat).

    Reversing the arguments returns a plausible wrong answer instead of raising,
    which is exactly why the wrapper exists — so assert the wrapper's order.
    """
    assert geo.timezone_at(51.5074, -0.1278) == "Europe/London"
    assert geo.timezone_at(40.7128, -74.0060) == "America/New_York"
    assert geo.timezone_at(35.6762, 139.6503) == "Asia/Tokyo"


def test_timezone_at_falls_back_to_a_nautical_zone_over_open_ocean():
    """Open ocean yields an Etc/GMT offset rather than None.

    Worth pinning: it means a stay always gets a usable zone, so the day-boundary
    logic never has to cope with a missing timezone.
    """
    assert geo.timezone_at(0.0, -140.0) == "Etc/GMT+9"


def test_perpendicular_distance_handles_degenerate_segment():
    p = (51.001, 0.0)
    assert geo._perpendicular_distance(p, LONDON, LONDON) == pytest.approx(
        geo.haversine_m(p, LONDON)
    )
