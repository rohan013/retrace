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
