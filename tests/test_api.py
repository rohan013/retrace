"""Query API behaviour."""

import pytest

from app import segment

from .conftest import BASE_TS, HOME, Track, offset_m

OFFICE = offset_m(HOME, 3_000, 2_000)


def seed(client, conn, track=None):
    (track or Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=3)).insert(conn)
    segment.rebuild(conn)


# -- days -------------------------------------------------------------------


def test_a_day_interleaves_stays_and_trips_in_order(client, conn):
    seed(client, conn)
    day = client.get("/api/v1/days/2026-06-01").json()

    kinds = [item["type"] for item in day["items"]]
    assert kinds == ["stay", "trip", "stay"]
    assert day["items"] == sorted(day["items"], key=lambda i: i["visible_start_ts"])


def test_a_day_summary_separates_moving_from_stationary(client, conn):
    seed(client, conn)
    summary = client.get("/api/v1/days/2026-06-01").json()["summary"]

    assert summary["stay_count"] == 2
    assert summary["trip_count"] == 1
    assert summary["distance_m"] > 3_000
    assert summary["time_stationary_s"] > summary["time_moving_s"]


def test_an_empty_day_returns_an_empty_feed(client, conn):
    seed(client, conn)
    day = client.get("/api/v1/days/2020-01-01").json()
    assert day["items"] == []
    assert day["summary"]["distance_m"] == 0


def test_a_malformed_date_is_rejected(client, conn):
    assert client.get("/api/v1/days/not-a-date").status_code == 400


def test_the_day_timezone_defaults_to_where_the_user_last_was(client, conn):
    seed(client, conn)
    assert client.get("/api/v1/days/2026-06-01").json()["tz"] == "Europe/London"


def test_the_timezone_can_be_overridden(client, conn):
    seed(client, conn)
    day = client.get("/api/v1/days/2026-06-01?tz=Asia/Tokyo").json()
    assert day["tz"] == "Asia/Tokyo"
    # Tokyo's midnight is nine hours earlier in absolute terms.
    london = client.get("/api/v1/days/2026-06-01?tz=Europe/London").json()
    assert day["start_ts"] < london["start_ts"]


def test_an_unknown_timezone_falls_back_rather_than_erroring(client, conn):
    seed(client, conn)
    assert client.get("/api/v1/days/2026-06-01?tz=Mars/Olympus").status_code == 200


# -- midnight ---------------------------------------------------------------


# BASE_TS is 2026-06-01 09:00 UTC, so a stay starting before UTC midnight begins
# on 31 May and runs into 1 June.
EVE = "2026-05-31"
MORNING = "2026-06-01"
UTC_MIDNIGHT = BASE_TS - (BASE_TS % 86400)


def test_a_stay_crossing_midnight_appears_on_both_days(client, conn):
    """Segmentation keeps it as one stay; only presentation splits it."""
    Track(start_ts=UTC_MIDNIGHT - 4 * 3600).stay(hours=8, interval=300).insert(conn)
    segment.rebuild(conn)

    first = client.get(f"/api/v1/days/{EVE}?tz=UTC").json()
    second = client.get(f"/api/v1/days/{MORNING}?tz=UTC").json()

    assert len(first["items"]) == 1
    assert len(second["items"]) == 1
    assert first["items"][0]["id"] == second["items"][0]["id"]

    # The later day knows it inherited the stay rather than starting one.
    assert second["items"][0]["continuation_of"] == EVE
    assert first["items"][0]["continuation_of"] is None


def test_a_midnight_crossing_stay_is_clipped_to_each_day(client, conn):
    Track(start_ts=UTC_MIDNIGHT - 4 * 3600).stay(hours=8, interval=300).insert(conn)
    segment.rebuild(conn)

    first = client.get(f"/api/v1/days/{EVE}?tz=UTC").json()["items"][0]
    second = client.get(f"/api/v1/days/{MORNING}?tz=UTC").json()["items"][0]

    # Each day sees only its own portion, and the two together make the whole.
    assert first["visible_duration_s"] + second["visible_duration_s"] == pytest.approx(
        first["duration_s"], abs=2
    )
    assert first["share"] + second["share"] == pytest.approx(1.0, abs=0.01)


def test_a_midnight_crossing_trip_splits_its_distance(client, conn):
    """Counting the whole drive on both days would double the year's mileage."""
    track = Track(start_ts=UTC_MIDNIGHT - 1800)
    track.move_to(offset_m(HOME, 40_000, 0), speed_mps=15, interval=30)
    track.insert(conn)
    segment.rebuild(conn)

    first = client.get(f"/api/v1/days/{EVE}?tz=UTC").json()
    second = client.get(f"/api/v1/days/{MORNING}?tz=UTC").json()

    total = first["summary"]["distance_m"] + second["summary"]["distance_m"]
    trip = client.get("/api/v1/trips").json()[0]
    assert total == pytest.approx(trip["distance_m"], rel=0.01)
    assert first["summary"]["distance_m"] > 0
    assert second["summary"]["distance_m"] > 0


# -- events -------------------------------------------------------------


def insert_event(conn, ts, kind, value_text=None, subject=None, device="phone", source="test"):
    conn.execute(
        "INSERT INTO events (ts, kind, source, subject, device, value_text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, kind, source, subject, device, value_text, ts),
    )


def test_a_paired_start_and_end_become_one_range(client, conn):
    insert_event(conn, BASE_TS, "app", "open", subject="Spotify")
    insert_event(conn, BASE_TS + 600, "app", "close", subject="Spotify")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 1
    assert events[0]["shape"] == "range"
    assert events[0]["start_ts"] == BASE_TS
    assert events[0]["end_ts"] == BASE_TS + 600
    assert events[0]["ongoing"] is False


def test_an_unmatched_start_is_ongoing(client, conn):
    insert_event(conn, BASE_TS, "wifi", "connected", subject="HomeWifi")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 1
    assert events[0]["shape"] == "range"
    assert events[0]["ongoing"] is True
    assert events[0]["end_ts"] is None


def test_an_unmatched_end_is_a_flagged_point(client, conn):
    insert_event(conn, BASE_TS, "wifi", "disconnected", subject="HomeWifi")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 1
    assert events[0]["shape"] == "point"
    assert events[0]["flagged"] is True


def test_a_repeated_start_before_any_end_is_ignored(client, conn):
    """Covers a double-fired automation: only the first start is used."""
    insert_event(conn, BASE_TS, "app", "open", subject="Spotify")
    insert_event(conn, BASE_TS + 60, "app", "open", subject="Spotify")
    insert_event(conn, BASE_TS + 600, "app", "close", subject="Spotify")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 1
    assert events[0]["start_ts"] == BASE_TS
    assert events[0]["end_ts"] == BASE_TS + 600


def test_a_range_spanning_midnight_shows_as_ongoing_on_the_earlier_day(client, conn):
    """Pairing looks backward from a day's own end. A range whose close lands on
    the following day is paired there, and reads as ongoing from here."""
    insert_event(conn, UTC_MIDNIGHT - 3600, "carplay", "connected")
    insert_event(conn, UTC_MIDNIGHT + 3600, "carplay", "disconnected")

    first = client.get(f"/api/v1/days/{EVE}?tz=UTC").json()
    first_event = [i for i in first["items"] if i["type"] == "event"][0]
    assert first_event["ongoing"] is True
    assert first_event["continuation_of"] is None


def test_a_range_spanning_midnight_is_paired_on_the_later_day(client, conn):
    insert_event(conn, UTC_MIDNIGHT - 3600, "carplay", "connected")
    insert_event(conn, UTC_MIDNIGHT + 3600, "carplay", "disconnected")

    second = client.get(f"/api/v1/days/{MORNING}?tz=UTC").json()
    second_event = [i for i in second["items"] if i["type"] == "event"][0]

    assert second_event["ongoing"] is False
    assert second_event["start_ts"] == UTC_MIDNIGHT - 3600
    assert second_event["end_ts"] == UTC_MIDNIGHT + 3600
    assert second_event["continuation_of"] == EVE


def test_an_unrecognised_kind_is_always_a_point(client, conn):
    insert_event(conn, BASE_TS, "workout", "started")
    insert_event(conn, BASE_TS + 600, "workout", "ended")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 2
    assert all(e["shape"] == "point" and not e["flagged"] for e in events)


def test_geofence_enter_leave_pairs_into_a_range(client, conn):
    insert_event(conn, BASE_TS, "geofence", "enter", subject="home")
    insert_event(conn, BASE_TS + 1800, "geofence", "leave", subject="home")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event"]

    assert len(events) == 1
    assert events[0]["shape"] == "range"
    assert events[0]["subject"] == "home"


def test_event_count_appears_in_the_summary(client, conn):
    insert_event(conn, BASE_TS, "app", "open", subject="Spotify")
    insert_event(conn, BASE_TS + 600, "app", "close", subject="Spotify")
    insert_event(conn, BASE_TS + 1200, "wifi", "disconnected")

    summary = client.get("/api/v1/days/2026-06-01").json()["summary"]
    assert summary["event_count"] == 2


def test_events_appear_alongside_stays_and_trips_sorted_by_time(client, conn):
    seed(client, conn)
    insert_event(conn, BASE_TS + 3600, "app", "open", subject="Spotify")
    insert_event(conn, BASE_TS + 3900, "app", "close", subject="Spotify")

    day = client.get("/api/v1/days/2026-06-01").json()
    assert "event" in {i["type"] for i in day["items"]}
    assert day["items"] == sorted(day["items"], key=lambda i: i["visible_start_ts"])


def test_events_endpoint_returns_a_plain_list(client, conn):
    assert client.get("/api/v1/events").json() == []


def test_events_filter_by_time_device_and_kind(client, conn):
    insert_event(conn, BASE_TS, "app", "open", subject="Spotify", device="iphone")
    insert_event(
        conn, BASE_TS + 100_000, "wifi", "connected", subject="Office", device="ipad"
    )

    by_device = client.get("/api/v1/events?device=iphone").json()
    assert {e["device"] for e in by_device} == {"iphone"}

    by_kind = client.get("/api/v1/events?kind=wifi").json()
    assert {e["kind"] for e in by_kind} == {"wifi"}

    later = client.get(f"/api/v1/events?from={BASE_TS + 50_000}").json()
    assert {e["device"] for e in later} == {"ipad"}


# -- macbook activity ---------------------------------------------------


def test_session_unlock_lock_pairs_into_a_range(client, conn):
    insert_event(conn, BASE_TS, "session", "unlock", device="macbook")
    insert_event(conn, BASE_TS + 3600, "session", "lock", device="macbook")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event" and i["kind"] == "session"]

    assert len(events) == 1
    assert events[0]["shape"] == "range"
    assert events[0]["subject"] is None
    assert events[0]["start_ts"] == BASE_TS
    assert events[0]["end_ts"] == BASE_TS + 3600


def test_session_tolerates_a_redundant_unlock_from_wake_and_screen_unlock(client, conn):
    """Wake and screen-unlock both firing produces two 'unlock' pings -- the
    second is noise, same tolerance the phone already relies on."""
    insert_event(conn, BASE_TS, "session", "unlock", device="macbook")
    insert_event(conn, BASE_TS + 2, "session", "unlock", device="macbook")
    insert_event(conn, BASE_TS + 3600, "session", "lock", device="macbook")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event" and i["kind"] == "session"]

    assert len(events) == 1
    assert events[0]["start_ts"] == BASE_TS


def test_session_a_redundant_lock_strands_as_a_flagged_point(client, conn):
    """Unlike a redundant start, a redundant end is NOT absorbed -- this is
    why the daemon must dedup sleep/lock itself rather than relying on
    server-side pairing tolerance."""
    insert_event(conn, BASE_TS, "session", "unlock", device="macbook")
    insert_event(conn, BASE_TS + 3600, "session", "lock", device="macbook")
    insert_event(conn, BASE_TS + 3602, "session", "lock", device="macbook")

    day = client.get("/api/v1/days/2026-06-01").json()
    events = [i for i in day["items"] if i["type"] == "event" and i["kind"] == "session"]

    ranges = [e for e in events if e["shape"] == "range"]
    points = [e for e in events if e["shape"] == "point"]
    assert len(ranges) == 1
    assert len(points) == 1
    assert points[0]["flagged"] is True


def test_focus_switching_apps_closes_the_previous_and_opens_the_next(client, conn):
    insert_event(conn, BASE_TS, "focus", "start", subject="Terminal", device="macbook")
    insert_event(conn, BASE_TS + 120, "focus", "end", subject="Terminal", device="macbook")
    insert_event(conn, BASE_TS + 120, "focus", "start", subject="Safari", device="macbook")

    day = client.get("/api/v1/days/2026-06-01").json()
    ranges = sorted(
        (i for i in day["items"] if i["type"] == "event" and i["kind"] == "focus"),
        key=lambda e: e["start_ts"],
    )

    assert len(ranges) == 2
    assert ranges[0]["subject"] == "Terminal"
    assert ranges[0]["end_ts"] == BASE_TS + 120
    assert ranges[0]["ongoing"] is False
    assert ranges[1]["subject"] == "Safari"
    assert ranges[1]["ongoing"] is True


def test_site_domain_switch_with_a_gap_emits_no_site_during_it(client, conn):
    """The gap stands in for an incognito window: the daemon emits nothing
    while mode == 'incognito', so no 'site' event exists for that stretch."""
    insert_event(conn, BASE_TS, "site", "start", subject="github.com", device="macbook")
    insert_event(conn, BASE_TS + 300, "site", "end", subject="github.com", device="macbook")
    insert_event(
        conn, BASE_TS + 900, "site", "start", subject="news.ycombinator.com", device="macbook"
    )
    insert_event(
        conn, BASE_TS + 1200, "site", "end", subject="news.ycombinator.com", device="macbook"
    )

    day = client.get("/api/v1/days/2026-06-01").json()
    ranges = sorted(
        (i for i in day["items"] if i["type"] == "event" and i["kind"] == "site"),
        key=lambda e: e["start_ts"],
    )

    assert {r["subject"] for r in ranges} == {"github.com", "news.ycombinator.com"}
    assert ranges[0]["end_ts"] < ranges[1]["start_ts"]


def test_a_device_that_only_sends_events_still_appears_in_the_device_list(client, conn):
    insert_event(conn, BASE_TS, "session", "unlock", device="macbook")
    insert_event(conn, BASE_TS + 3600, "session", "lock", device="macbook")

    devices = {d["device"]: d for d in client.get("/api/v1/devices").json()}
    assert "macbook" in devices
    assert devices["macbook"]["points"] == 0
    assert devices["macbook"]["events"] == 2


# -- points -----------------------------------------------------------------


def test_the_ingest_path_refuses_to_read_anything_back(client, conn):
    """The one path guarded by a machine credential instead of a human login
    must never serve data back out, so a leaked or shared credential can only
    ever be used to write, never to read the location history.
    """
    seed(client, conn)

    assert client.get("/api/v1/locations").status_code == 405
    assert client.get("/api/v1/locations?limit=10").status_code == 405
    assert len(client.get("/api/v1/points?limit=10").json()["points"]) == 10


def test_locations_paginate_by_id(client, conn):
    seed(client, conn)

    first = client.get("/api/v1/points?limit=10").json()
    assert len(first["points"]) == 10
    assert first["complete"] is False

    second = client.get(f"/api/v1/points?limit=10&since_id={first['next_since_id']}").json()
    assert second["points"][0]["id"] > first["points"][-1]["id"]


def test_paginating_to_the_end_terminates(client, conn):
    seed(client, conn)

    seen, since, guard = [], 0, 0
    while guard < 100:
        page = client.get(f"/api/v1/points?limit=25&since_id={since}").json()
        seen.extend(page["points"])
        if page["complete"]:
            break
        since = page["next_since_id"]
        guard += 1

    total = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]
    assert len(seen) == total
    assert len({p["id"] for p in seen}) == total


def test_flagged_points_can_be_excluded_but_are_included_by_default(client, conn):
    Track().stay(minutes=30).outlier(north_m=90_000).stay(minutes=30).insert(conn)
    segment.rebuild(conn)

    everything = client.get("/api/v1/points?limit=50000").json()["points"]
    trusted = client.get("/api/v1/points?limit=50000&include_flagged=false").json()["points"]

    assert len(everything) == len(trusted) + 1
    assert any(p["anomaly"] for p in everything)
    assert not any(p["anomaly"] for p in trusted)


def test_locations_can_be_simplified_server_side(client, conn):
    Track().move_to(offset_m(HOME, 5_000, 0), speed_mps=13, interval=5).insert(conn)

    full = client.get("/api/v1/points?limit=50000").json()["points"]
    thinned = client.get("/api/v1/points?limit=50000&simplify_m=50").json()["points"]

    assert len(thinned) < len(full)
    assert thinned[0]["id"] == full[0]["id"]


def test_locations_filter_by_time_and_device(client, conn):
    Track(device="phone").stay(hours=1).insert(conn)
    Track(device="watch", start_ts=BASE_TS + 100_000).stay(hours=1).insert(conn)

    phone = client.get("/api/v1/points?device=phone&limit=50000").json()["points"]
    assert {p["device"] for p in phone} == {"phone"}

    later = client.get(f"/api/v1/points?from={BASE_TS + 100_000}&limit=50000").json()["points"]
    assert {p["device"] for p in later} == {"watch"}


# -- naming -----------------------------------------------------------------


def test_naming_a_stay_creates_a_place_and_names_the_other_visits(client, conn):
    Track().stay(hours=2).insert(conn)
    Track(start_ts=BASE_TS + 300_000).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    stays = client.get("/api/v1/stays").json()
    assert len(stays) == 2

    client.patch(f"/api/v1/stays/{stays[0]['id']}", json={"name": "Home"})

    named = client.get("/api/v1/stays").json()
    assert all(s["place_name"] == "Home" for s in named)


def test_a_named_place_is_locked_against_later_guesses(client, conn):
    seed(client, conn)
    stays = client.get("/api/v1/stays").json()
    client.patch(f"/api/v1/stays/{stays[0]['id']}", json={"name": "Home"})

    place = client.get("/api/v1/places").json()[0]
    assert place["name_locked_at"] is not None
    assert place["source"] == "manual"


def test_a_note_survives_a_rebuild(client, conn):
    seed(client, conn)
    stays = client.get("/api/v1/stays").json()
    client.patch(f"/api/v1/stays/{stays[0]['id']}", json={"note": "dentist"})

    client.post("/api/v1/reprocess")
    assert conn.execute("SELECT note FROM stay_notes").fetchone()["note"] == "dentist"


def test_naming_a_missing_stay_is_a_404(client, conn):
    assert client.patch("/api/v1/stays/999", json={"name": "Nowhere"}).status_code == 404


def test_an_empty_name_is_rejected(client, conn):
    seed(client, conn)
    stays = client.get("/api/v1/stays").json()
    response = client.patch(f"/api/v1/stays/{stays[0]['id']}", json={"name": "   "})
    assert response.status_code == 400


# -- places and areas -------------------------------------------------------


def test_places_report_visit_counts_and_time(client, conn):
    Track().stay(hours=2).insert(conn)
    Track(start_ts=BASE_TS + 300_000).stay(hours=3).insert(conn)
    segment.rebuild(conn)

    client.post("/api/v1/places", json={"name": "Home", "lat": HOME[0], "lon": HOME[1]})
    place = client.get("/api/v1/places").json()[0]

    assert place["visit_count"] == 2
    assert place["total_seconds"] > 4 * 3600


def test_creating_a_place_requires_its_fields(client, conn):
    assert client.post("/api/v1/places", json={"name": "x"}).status_code == 400


def test_places_can_be_renamed_and_deleted(client, conn):
    created = client.post(
        "/api/v1/places", json={"name": "Old", "lat": HOME[0], "lon": HOME[1]}
    ).json()

    renamed = client.patch(f"/api/v1/places/{created['id']}", json={"name": "New"}).json()
    assert renamed["name"] == "New"

    assert client.delete(f"/api/v1/places/{created['id']}").status_code == 204
    assert client.get("/api/v1/places").json() == []


def test_areas_can_be_created_and_take_effect_on_rebuild(client, conn):
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)
    assert client.get("/api/v1/stays").json()[0]["area_id"] is None

    area = client.post(
        "/api/v1/areas",
        json={
            "name": "Home",
            "min_lat": HOME[0] - 0.001,
            "min_lon": HOME[1] - 0.001,
            "max_lat": HOME[0] + 0.001,
            "max_lon": HOME[1] + 0.001,
        },
    )
    assert area.status_code == 201
    client.post("/api/v1/reprocess")

    assert client.get("/api/v1/stays").json()[0]["area_id"] == area.json()["id"]


def test_creating_an_area_requires_its_fields(client, conn):
    assert client.post("/api/v1/areas", json={"name": "x"}).status_code == 400


def test_an_area_with_reversed_corners_is_rejected(client, conn):
    response = client.post(
        "/api/v1/areas",
        json={
            "name": "x",
            "min_lat": HOME[0] + 0.001,  # swapped
            "min_lon": HOME[1],
            "max_lat": HOME[0],
            "max_lon": HOME[1] + 0.001,
        },
    )
    assert response.status_code == 400


# -- operations -------------------------------------------------------------


def test_stats_summarise_a_range(client, conn):
    seed(client, conn)
    stats = client.get("/api/v1/stats").json()

    assert stats["stays"] == 2
    assert stats["trips"] == 1
    assert stats["distance_m"] > 3_000
    assert stats["points"] > 0


def test_reprocess_rebuilds_and_reports(client, conn):
    seed(client, conn)
    result = client.post("/api/v1/reprocess").json()

    assert result["stays"] == 2
    assert result["trips"] == 1
    assert result["devices"] == ["phone"]


def test_devices_are_listed_with_their_extent(client, conn):
    Track(device="phone").stay(hours=1).insert(conn)
    Track(device="watch").stay(hours=1).insert(conn)

    devices = {d["device"]: d for d in client.get("/api/v1/devices").json()}
    assert set(devices) == {"phone", "watch"}
    assert devices["phone"]["points"] > 0
    assert devices["phone"]["first_ts"] <= devices["phone"]["last_ts"]


def test_healthz_reports_liveness_and_freshness(client, conn):
    seed(client, conn)
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["points"] > 0
    assert health["latest_point_ts"] is not None


def test_the_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# -- background sweep -------------------------------------------------------


def test_the_sweep_rebuilds_what_ingest_marked_stale(client, conn, db_path):
    from app.main import sweep_once

    client.post("/api/v1/locations", json=Track().stay(hours=2).payload())
    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 0

    result = sweep_once()
    assert result is not None
    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 1


def test_the_sweep_is_a_no_op_when_nothing_changed(client, conn, db_path):
    from app.main import sweep_once

    client.post("/api/v1/locations", json=Track().stay(hours=2).payload())
    sweep_once()
    assert sweep_once() is None
