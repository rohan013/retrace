"""The day breakdown: collapsing overlapping streams into one partition.

Every case here asserts the same invariant somewhere -- the parts total the day
exactly -- because that is the whole point of the module. A breakdown that adds
up to 26 hours is not a rounding problem, it is a claim about the day that
cannot be true.
"""

from app import config
from app.breakdown import day_breakdown

DAY_START = 1780272000  # 2026-06-01 00:00:00 UTC
DAY_END = DAY_START + 86400


def at(hour: float) -> int:
    return DAY_START + int(hour * 3600)


def stay(start_h, end_h, name=None, place_id=None, area_id=None):
    return {
        "type": "stay",
        "name": name,
        "place_id": place_id,
        "area_id": area_id,
        "lat": 51.5,
        "lon": -0.12,
        "visible_start_ts": at(start_h),
        "visible_end_ts": at(end_h),
    }


def trip(start_h, end_h, point_count=200):
    return {
        "type": "trip",
        "point_count": point_count,
        "duration_s": at(end_h) - at(start_h),
        "visible_start_ts": at(start_h),
        "visible_end_ts": at(end_h),
    }


def event(kind, start_h, end_h, subject=None):
    return {
        "type": "event",
        "shape": "range",
        "kind": kind,
        "subject": subject,
        "visible_start_ts": at(start_h),
        "visible_end_ts": at(end_h),
    }


def run(items):
    return day_breakdown(items, DAY_START, DAY_END)


def places(result):
    return {p["label"]: p["seconds"] for p in result["places"]}


def activities(result):
    return {a["label"]: a["seconds"] for a in result["activities"]}


def assert_totals(result):
    """Places, activities and the nested grid must each account for the day."""
    total = result["total_s"]
    assert total == DAY_END - DAY_START
    assert sum(p["seconds"] for p in result["places"]) == total
    assert sum(a["seconds"] for a in result["activities"]) == total
    assert sum(a["seconds"] for p in result["places"] for a in p["activities"]) == total


# -- the partition holds ----------------------------------------------------


def test_a_day_holding_nothing_is_still_a_whole_day():
    result = run([])
    assert_totals(result)
    assert places(result) == {"No location": 86400}
    assert activities(result) == {"Untracked": 86400}


def test_a_full_day_of_streams_still_totals_the_day():
    result = run(
        [
            stay(0, 9, name="Home"),
            trip(9, 9.5),
            stay(9.5, 18, name="Work"),
            trip(18, 18.5),
            stay(18.5, 24, name="Home"),
            event("sleep", 0, 7.5),
            event("app", 8, 8.5, subject="reddit"),
            event("focus", 10, 12, subject="iTerm2"),
            event("site", 10.5, 11, subject="reddit.com"),
        ]
    )
    assert_totals(result)
    assert places(result)["Home"] == int(14.5 * 3600)
    assert places(result)["Work"] == int(8.5 * 3600)


def test_a_short_day_totals_the_short_day():
    """A spring-forward day is 23 hours, and the parts must add to 23."""
    result = day_breakdown([stay(0, 5, name="Home")], DAY_START, DAY_START + 23 * 3600)
    assert result["total_s"] == 23 * 3600
    assert sum(p["seconds"] for p in result["places"]) == 23 * 3600


# -- priority between streams ------------------------------------------------


def test_sleep_wins_the_hour_but_the_stay_keeps_the_place():
    """Being asleep is what you were doing; it is not where you were."""
    result = run([stay(0, 8, name="Home"), event("sleep", 0, 8)])
    assert_totals(result)
    home = next(p for p in result["places"] if p["label"] == "Home")
    assert home["seconds"] == 8 * 3600
    assert {a["label"]: a["seconds"] for a in home["activities"]}["Sleep"] == 8 * 3600


def test_a_site_inside_its_browser_is_counted_once_not_twice():
    """A site range always sits inside a focus range. The specific one wins and
    the browser keeps only the time no site was open."""
    result = run(
        [
            event("focus", 9, 11, subject="Google Chrome"),
            event("site", 9.5, 10, subject="reddit.com"),
        ]
    )
    assert_totals(result)
    totals = activities(result)
    assert totals["Reddit"] == 1800
    assert totals["Other"] == 5400  # the 1.5h of Chrome with no tracked site


def test_the_macbook_outranks_a_phone_app_open_at_the_same_time():
    """A phone `app` range can sit stale for hours; frontmost-on-the-Mac cannot."""
    result = run(
        [
            event("app", 9, 12, subject="reddit"),
            event("focus", 10, 11, subject="iTerm2"),
        ]
    )
    assert_totals(result)
    totals = activities(result)
    assert totals["Other"] == 3600  # the hour the MacBook claimed
    assert totals["Reddit"] == 2 * 3600


def test_of_two_overlapping_phone_apps_the_later_one_wins():
    """The phone reports one app at a time but its ranges can overlap anyway, so
    a range that has been open for hours must not suppress what replaced it."""
    result = run(
        [
            event("app", 0, 12, subject="chrome"),
            event("app", 9, 10, subject="reddit"),
        ]
    )
    assert_totals(result)
    totals = activities(result)
    assert totals["Reddit"] == 3600
    assert totals["Chrome"] == 11 * 3600


def test_time_with_no_stream_at_all_reads_as_untracked():
    result = run([stay(0, 24, name="Home"), event("app", 9, 10, subject="reddit")])
    assert_totals(result)
    assert activities(result)["Untracked"] == 23 * 3600


# -- naming activities --------------------------------------------------------


def test_reddit_from_the_phone_and_the_macbook_are_one_slice():
    result = run(
        [
            event("app", 1, 2, subject="reddit"),
            event("site", 9, 10, subject="reddit.com"),
        ]
    )
    assert_totals(result)
    assert activities(result)["Reddit"] == 2 * 3600


def test_a_subdomain_lands_on_its_sites_slice():
    result = run([event("site", 9, 10, subject="old.reddit.com")])
    assert_totals(result)
    assert activities(result)["Reddit"] == 3600


def test_an_untracked_subject_lands_in_other_rather_than_its_own_slice():
    result = run(
        [
            event("focus", 9, 10, subject="Sublime Text"),
            event("site", 11, 12, subject="news.ycombinator.com"),
            event("app", 13, 14, subject="carplay"),
        ]
    )
    assert_totals(result)
    totals = activities(result)
    assert totals["Other"] == 3 * 3600
    assert "Sublime Text" not in totals


def test_an_unpaired_point_event_contributes_no_time():
    """Only ranges have duration. A flagged point is a zero-width observation."""
    items = [{"type": "event", "shape": "point", "kind": "app", "subject": "reddit",
              "visible_start_ts": at(9), "visible_end_ts": at(9)}]
    result = run(items)
    assert_totals(result)
    assert activities(result) == {"Untracked": 86400}


# -- places -------------------------------------------------------------------


def test_a_stay_with_no_name_is_unnamed_not_unknown():
    """Somewhere unrecognised is still somewhere; only absent data is unknown."""
    result = run([stay(0, 6)])
    assert_totals(result)
    assert places(result) == {"Unnamed": 6 * 3600, "No location": 18 * 3600}


def test_a_stay_carries_its_identity_through_for_colouring():
    result = run([stay(0, 6, name="Home", area_id=5)])
    home = next(p for p in result["places"] if p["label"] == "Home")
    assert home["area_id"] == 5
    assert home["lat"] == 51.5


def test_a_dense_trip_is_movement():
    result = run([trip(9, 10, point_count=120)])
    assert_totals(result)
    assert places(result)["Moving"] == 3600


def test_a_trip_too_sparse_to_witness_is_unknown_location_not_movement():
    """A phone that goes dark for a day and comes back elsewhere produces one
    enormous trip. Two fixes twenty hours apart say nothing about the twenty
    hours, so reporting them as a drive invents a journey."""
    result = run([trip(2, 22, point_count=2)])
    assert_totals(result)
    assert places(result) == {"No location": 86400}


def test_the_density_floor_is_tunable(monkeypatch):
    sparse = [trip(9, 10, point_count=10)]
    monkeypatch.setattr(config, "BREAKDOWN_TRIP_MIN_FIXES_PER_HOUR", 4.0)
    assert places(run(sparse))["Moving"] == 3600
    monkeypatch.setattr(config, "BREAKDOWN_TRIP_MIN_FIXES_PER_HOUR", 40.0)
    assert "Moving" not in places(run(sparse))


def test_overlapping_stays_from_two_devices_are_not_double_counted():
    result = run([stay(0, 12, name="Home"), stay(6, 18, name="Work")])
    assert_totals(result)
    assert places(result) == {"Home": 6 * 3600, "Work": 12 * 3600, "No location": 6 * 3600}
