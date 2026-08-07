import json

import pytest

from app import ingest
from app.providers import detect, get
from app.providers.owntracks import OwnTracksProvider

from .conftest import BASE_TS, HOME, TEST_TOKEN, Track


def owntracks_payload(**overrides):
    payload = {
        "_type": "location",
        "tid": "RX",
        "lat": HOME[0],
        "lon": HOME[1],
        "tst": BASE_TS,
        "acc": 12,
        "alt": 35,
        "vac": 6,
        "vel": 36,  # km/h
        "cog": 180,
        "batt": 84,
        "bs": 2,
        "conn": "w",
        "t": "p",
        "m": 2,
        "p": 101.2,
        "SSID": "home-wifi",
        "BSSID": "aa:bb:cc:dd:ee:ff",
        "inregions": ["home"],
        "topic": "owntracks/rohan/iphone",
    }
    payload.update(overrides)
    return payload


# -- protocol conformance ---------------------------------------------------


def test_owntracks_gets_a_bare_json_array_back(client):
    """OwnTracks reads the response body as a list of commands.

    Returning an object makes the client treat a successful POST as failed and
    retry forever, so the array shape is load-bearing, not cosmetic.
    """
    response = client.post("/api/v1/locations", json=owntracks_payload())
    assert response.status_code == 200
    assert response.json() == []


def test_generic_format_gets_a_rest_shaped_body(client):
    track = Track().stay(minutes=5, interval=60)
    response = client.post("/api/v1/locations", json=track.payload())
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == len(track.points())
    assert body["duplicates"] == 0


def test_velocity_is_converted_from_kmh_to_ms(conn):
    """OwnTracks reports km/h. Everything downstream assumes m/s."""
    parsed = OwnTracksProvider().parse(owntracks_payload(vel=36), {})
    assert parsed.points[0].speed_mps == pytest.approx(10.0)


def test_negative_velocity_means_unknown_not_reverse(conn):
    parsed = OwnTracksProvider().parse(owntracks_payload(vel=-1), {})
    assert parsed.points[0].speed_mps is None


def test_owntracks_fields_are_preserved(client, conn):
    client.post("/api/v1/locations", json=owntracks_payload())
    row = conn.execute("SELECT * FROM points").fetchone()

    assert row["accuracy"] == 12
    assert row["altitude"] == 35
    assert row["vertical_accuracy"] == 6
    assert row["heading"] == 180
    assert row["battery"] == 84
    assert row["battery_status"] == "charging"
    assert row["connection"] == "wifi"
    assert row["trigger_type"] == "p"
    assert row["ssid"] == "home-wifi"
    assert row["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert row["pressure"] == 101.2  # iOS barometer
    assert row["monitoring_mode"] == 2
    assert json.loads(row["in_regions"]) == ["home"]
    assert row["source"] == "owntracks"


def test_raw_payload_is_retained_verbatim(client, conn):
    payload = owntracks_payload()
    client.post("/api/v1/locations", json=payload)
    stored = json.loads(conn.execute("SELECT raw FROM points").fetchone()["raw"])
    assert stored == payload


# -- device identity --------------------------------------------------------


def test_device_comes_from_the_x_limit_d_header_first(client, conn):
    client.post(
        "/api/v1/locations", json=owntracks_payload(), headers={"X-Limit-D": "iphone-15"}
    )
    assert conn.execute("SELECT device FROM points").fetchone()["device"] == "iphone-15"


def test_device_falls_back_to_the_topic_then_the_tracker_id():
    provider = OwnTracksProvider()

    from_topic = provider.parse(owntracks_payload(), {})
    assert from_topic.points[0].device == "iphone"

    payload = owntracks_payload()
    del payload["topic"]
    from_tid = provider.parse(payload, {})
    assert from_tid.points[0].device == "RX"


# -- non-location message types ---------------------------------------------


def test_geofence_transitions_become_events(client, conn):
    client.post(
        "/api/v1/locations",
        json={
            "_type": "transition",
            "event": "enter",
            "desc": "home",
            "lat": HOME[0],
            "lon": HOME[1],
            "tst": BASE_TS,
            "tid": "RX",
        },
    )
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["kind"] == "geofence"
    assert row["subject"] == "home"
    assert row["value_text"] == "enter"
    assert row["device"] == "RX"
    assert conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == 0


@pytest.mark.parametrize("kind", ["lwt", "waypoint", "card", "cmd", "status"])
def test_other_owntracks_message_types_are_accepted_and_discarded(client, conn, kind):
    """These arrive at the same endpoint and are not errors."""
    response = client.post("/api/v1/locations", json={"_type": kind, "tst": BASE_TS})
    assert response.status_code == 200
    assert conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == 0


# -- shortcuts provider ------------------------------------------------------


def test_shortcuts_payload_becomes_an_event(client, conn):
    response = client.post(
        "/api/v1/locations?format=shortcuts",
        json={
            "kind": "app",
            "subject": "Spotify",
            "value": "open",
            "device": "iphone",
            "ts": BASE_TS,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "events": 1}

    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["kind"] == "app"
    assert row["subject"] == "Spotify"
    assert row["value_text"] == "open"
    assert row["device"] == "iphone"
    assert row["ts"] == BASE_TS
    assert row["source"] == "shortcuts"


def test_shortcuts_ts_defaults_to_receive_time(client, conn):
    import time

    client.post(
        "/api/v1/locations?format=shortcuts",
        json={"kind": "carplay", "value": "connected", "device": "iphone"},
    )
    row = conn.execute("SELECT ts FROM events").fetchone()
    assert abs(row["ts"] - int(time.time())) < 5


def test_shortcuts_device_falls_back_to_header_then_default():
    from app.providers.shortcuts import ShortcutsProvider

    provider = ShortcutsProvider()

    from_header = provider.parse({"kind": "app", "value": "open"}, {"x-device": "ipad"})
    assert from_header.events[0].device == "ipad"

    from_default = provider.parse({"kind": "app", "value": "open"}, {})
    assert from_default.events[0].device == "unknown"


def test_shortcuts_detection_does_not_collide_with_other_providers():
    assert detect({"kind": "app", "value": "open"}).name == "shortcuts"
    assert detect({"_type": "location"}).name == "owntracks"
    assert detect({"points": []}).name == "generic"


def test_replaying_a_shortcuts_event_stores_it_once(client, conn):
    payload = {
        "kind": "app",
        "subject": "Spotify",
        "value": "open",
        "device": "iphone",
        "ts": BASE_TS,
    }
    client.post("/api/v1/locations?format=shortcuts", json=payload)
    client.post("/api/v1/locations?format=shortcuts", json=payload)
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1


def test_shortcuts_events_from_two_devices_are_not_deduped_together(client, conn):
    base = {"kind": "battery", "value": "charging", "ts": BASE_TS}
    client.post("/api/v1/locations?format=shortcuts", json={**base, "device": "iphone"})
    client.post("/api/v1/locations?format=shortcuts", json={**base, "device": "ipad"})
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 2


# -- idempotency ------------------------------------------------------------


def test_posting_the_same_batch_twice_stores_it_once(client, conn):
    track = Track().stay(minutes=10, interval=60)
    payload = track.payload()

    first = client.post("/api/v1/locations", json=payload).json()
    second = client.post("/api/v1/locations", json=payload).json()

    assert first["accepted"] == len(track.points())
    assert second["accepted"] == 0
    assert second["duplicates"] == len(track.points())
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == len(track.points())
    )


def test_the_same_timestamp_from_two_devices_is_two_points(client, conn):
    payload = {
        "points": [
            {"device": "phone", "ts": BASE_TS, "lat": HOME[0], "lon": HOME[1]},
            {"device": "watch", "ts": BASE_TS, "lat": HOME[0], "lon": HOME[1]},
        ]
    }
    client.post("/api/v1/locations", json=payload)
    assert conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == 2


def test_replaying_an_event_stores_it_once(client, conn):
    transition = {
        "_type": "transition",
        "event": "leave",
        "desc": "office",
        "tst": BASE_TS,
        "lat": HOME[0],
        "lon": HOME[1],
    }
    client.post("/api/v1/locations", json=transition)
    client.post("/api/v1/locations", json=transition)
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1


# -- batches ----------------------------------------------------------------


def test_a_top_level_list_is_treated_as_a_batch(client, conn):
    batch = [owntracks_payload(tst=BASE_TS + i * 60) for i in range(5)]
    response = client.post("/api/v1/locations", json=batch)
    assert response.json() == []
    assert conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == 5


# -- auth -------------------------------------------------------------------


def test_missing_credentials_are_rejected(anon_client):
    response = anon_client.post("/api/v1/locations", json=owntracks_payload())
    assert response.status_code == 401


def test_a_wrong_token_is_rejected(anon_client):
    response = anon_client.post(
        "/api/v1/locations",
        json=owntracks_payload(),
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_http_basic_is_accepted_because_owntracks_ios_cannot_set_headers(anon_client):
    """OwnTracks iOS offers username/password fields but no custom headers."""
    response = anon_client.post(
        "/api/v1/locations", json=owntracks_payload(), auth=("phone", TEST_TOKEN)
    )
    assert response.status_code == 200


def test_a_query_parameter_token_is_refused(anon_client):
    """uvicorn logs the query string, so a token sent that way lands in the
    journal in cleartext. It has to be a header."""
    response = anon_client.post(
        f"/api/v1/locations?token={TEST_TOKEN}", json=owntracks_payload()
    )
    assert response.status_code == 401


def test_ingest_fails_closed_when_no_token_is_configured(anon_client, monkeypatch):
    """An unset token must not silently leave a public write endpoint open."""
    from app import config

    monkeypatch.setattr(config, "INGEST_TOKEN", "")
    response = anon_client.post("/api/v1/locations", json=owntracks_payload())
    assert response.status_code == 503


# -- malformed input --------------------------------------------------------


def test_an_unrecognised_payload_is_rejected(client):
    response = client.post("/api/v1/locations", json={"something": "else"})
    assert response.status_code == 400


def test_points_missing_coordinates_are_skipped_not_fatal(client, conn):
    response = client.post(
        "/api/v1/locations",
        json={
            "points": [
                {"device": "phone", "ts": BASE_TS, "lat": HOME[0]},  # no lon
                {"device": "phone", "ts": BASE_TS + 60, "lat": HOME[0], "lon": HOME[1]},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_format_override_bypasses_detection(client, conn):
    response = client.post(
        "/api/v1/locations?format=generic",
        json={"points": [{"device": "phone", "ts": BASE_TS, "lat": 51.5, "lon": -0.1}]},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_an_unknown_format_override_is_rejected(client):
    response = client.post("/api/v1/locations?format=nonsense", json={"points": []})
    assert response.status_code == 400


# -- detection --------------------------------------------------------------


def test_detection_picks_the_right_provider():
    assert detect(owntracks_payload()).name == "owntracks"
    assert detect({"points": []}).name == "generic"
    assert detect({"nope": 1}) is None
    assert get("owntracks") is not None
    assert get("missing") is None


# -- dirty marking ----------------------------------------------------------


def test_ingest_records_the_earliest_stale_timestamp(conn):
    Track(start_ts=BASE_TS + 10_000).stay(minutes=5).insert(conn)
    assert int(ingest.take_dirty_from(conn)) == BASE_TS + 10_000

    Track(start_ts=BASE_TS).stay(minutes=5).insert(conn)
    Track(start_ts=BASE_TS + 50_000).stay(minutes=5).insert(conn)
    # A later arrival must not narrow the window that needs rebuilding.
    assert int(ingest.take_dirty_from(conn)) == BASE_TS


def test_taking_the_dirty_marker_clears_it(conn):
    Track().stay(minutes=5).insert(conn)
    assert ingest.take_dirty_from(conn) is not None
    assert ingest.take_dirty_from(conn) is None
