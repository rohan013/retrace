"""Rebuild semantics.

The derived layer is disposable by design: nothing in `points` records which stay
claimed it, so a stay can always grow, shrink or be re-evaluated when new data
arrives. These tests pin the consequences of that choice — particularly the
retroactive cases, which are exactly what an implementation that stamps points
with a stay id cannot do.
"""

import pytest

from app import db, ingest, segment

from .conftest import BASE_TS, HOME, Track, offset_m

OFFICE = offset_m(HOME, 3_000, 2_000)


def snapshot(conn):
    stays = [
        tuple(row)
        for row in conn.execute(
            "SELECT device, start_ts, end_ts, center_lat, center_lon, radius_m, "
            "point_count, confidence FROM stays ORDER BY device, start_ts"
        )
    ]
    trips = [
        tuple(row)
        for row in conn.execute(
            "SELECT device, start_ts, end_ts, distance_m, point_count "
            "FROM trips ORDER BY device, start_ts"
        )
    ]
    return stays, trips


# -- idempotency ------------------------------------------------------------


def test_rebuilding_twice_produces_identical_rows(conn):
    Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=2).insert(conn)

    segment.rebuild(conn)
    first = snapshot(conn)
    segment.rebuild(conn)
    second = snapshot(conn)

    assert first == second


def test_rebuilding_does_not_accumulate_duplicates(conn):
    Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=2).insert(conn)

    for _ in range(4):
        segment.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"] == 1


def test_reingesting_the_same_points_changes_nothing(conn):
    track = Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=2)
    track.insert(conn)
    segment.rebuild(conn)
    before = snapshot(conn)

    track.insert(conn)  # exact replay
    segment.rebuild(conn)

    assert snapshot(conn) == before


# -- retroactive data -------------------------------------------------------


def test_late_arriving_points_extend_an_existing_stay(conn):
    """The case a stay-id-stamping design cannot handle.

    A phone that was offline uploads its backlog after the fact. The stay it
    belongs to must grow to include it rather than a second stay appearing beside
    it.
    """
    Track().stay(hours=1, interval=60).insert(conn)
    segment.rebuild(conn)

    original = conn.execute("SELECT * FROM stays").fetchone()
    assert original["point_count"] > 0

    # The same visit continued; these fixes arrive late.
    late = Track(start_ts=original["end_ts"] + 60).stay(minutes=45, interval=60)
    late.insert(conn)
    segment.rebuild(conn, from_ts=ingest.take_dirty_from(conn))

    stays = conn.execute("SELECT * FROM stays").fetchall()
    assert len(stays) == 1
    assert stays[0]["point_count"] > original["point_count"]
    assert stays[0]["end_ts"] > original["end_ts"]


def test_a_point_arriving_in_the_middle_can_split_a_stay(conn):
    """Backfilled data may reveal the user left and came back.

    The derived layer must be free to conclude the opposite of what it concluded
    before, which only works because it is rebuilt rather than amended.
    """
    track = Track().stay(minutes=30, interval=60)
    resume_ts = track.ts + 7200
    Track(start_ts=resume_ts).stay(minutes=30, interval=60).insert(conn)
    track.insert(conn)
    segment.rebuild(conn)

    # Nothing observed in between, and the two clusters are colocated, so the
    # gap rule joins them.
    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 1

    # Now a fix appears mid-gap, far away: the user did leave.
    away = Track(start_ts=resume_ts - 3600)
    away.pos = OFFICE
    away.point(accuracy=10.0)
    away.insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM stays").fetchone()["n"] == 2


# -- windowing --------------------------------------------------------------


def test_an_incremental_rebuild_leaves_older_derived_rows_alone(conn):
    Track(start_ts=BASE_TS).stay(hours=2).insert(conn)
    Track(start_ts=BASE_TS + 200_000).stay(hours=2).insert(conn)
    segment.rebuild(conn)

    old = conn.execute("SELECT * FROM stays ORDER BY start_ts").fetchone()
    old_id, old_created = old["id"], old["created_at"]

    segment.rebuild(conn, from_ts=BASE_TS + 200_000)

    still_there = conn.execute(
        "SELECT * FROM stays WHERE id = ?", (old_id,)
    ).fetchone()
    assert still_there is not None
    assert still_there["created_at"] == old_created


def test_a_stay_overlapping_the_window_edge_is_rebuilt_whole(conn):
    """A partial rebuild must not truncate a stay to whatever fell inside it.

    The window is widened backwards to cover any derived row it would otherwise
    bisect, so a long overnight stay survives a rebuild aimed at the morning.
    """
    Track(start_ts=BASE_TS).stay(hours=10, interval=120).insert(conn)
    segment.rebuild(conn)

    original = conn.execute("SELECT * FROM stays").fetchone()

    # Aim a rebuild at the middle of that stay.
    segment.rebuild(conn, from_ts=BASE_TS + 5 * 3600)

    rebuilt = conn.execute("SELECT * FROM stays").fetchall()
    assert len(rebuilt) == 1
    assert rebuilt[0]["start_ts"] == original["start_ts"]
    assert rebuilt[0]["point_count"] == original["point_count"]


def test_the_cursor_advances_to_the_latest_point(conn):
    Track().stay(hours=1).insert(conn)
    segment.rebuild(conn)

    latest = conn.execute("SELECT MAX(ts) AS ts FROM points").fetchone()["ts"]
    assert int(db.get_state(conn, segment.CURSOR_KEY)) == latest


def test_rebuild_reports_what_it_did(conn):
    Track().stay(hours=1).move_to(OFFICE, speed_mps=12).stay(hours=1).insert(conn)
    result = segment.rebuild(conn)

    assert result.stays == 2
    assert result.trips == 1
    assert result.devices == ["phone"]


def test_rebuilding_an_empty_database_is_harmless(conn):
    result = segment.rebuild(conn)
    assert result.stays == 0
    assert result.trips == 0


# -- user edits survive -----------------------------------------------------


def test_places_survive_a_rebuild(conn):
    """User naming lives outside the derived layer and is never overwritten."""
    import time

    now = int(time.time())
    conn.execute(
        "INSERT INTO places (name, lat, lon, source, name_locked_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'manual', ?, ?, ?)",
        ("Home", HOME[0], HOME[1], now, now, now),
    )
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)
    segment.rebuild(conn)

    place = conn.execute("SELECT * FROM places").fetchone()
    assert place["name"] == "Home"
    assert place["name_locked_at"] == now


def test_stay_notes_survive_a_rebuild(conn):
    import time

    now = int(time.time())
    conn.execute(
        "INSERT INTO stay_notes (device, anchor_ts, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("phone", BASE_TS + 1800, "dentist", now, now),
    )
    Track().stay(hours=2).insert(conn)
    segment.rebuild(conn)

    assert conn.execute("SELECT note FROM stay_notes").fetchone()["note"] == "dentist"


def test_raw_points_are_never_deleted_by_a_rebuild(conn):
    Track().stay(hours=2).move_to(OFFICE, speed_mps=12).stay(hours=1).insert(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]

    for _ in range(3):
        segment.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"] == before
