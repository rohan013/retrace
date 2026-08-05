"""Schema migrations."""

import sqlite3
import time

import pytest

from app import db


def test_a_fresh_database_ends_up_on_the_latest_schema_version(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_an_area_with_reversed_corners_is_rejected_by_the_schema(tmp_path):
    path = str(tmp_path / "areas.db")
    db.init_db(path)
    conn = sqlite3.connect(path)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO areas (name, min_lat, min_lon, max_lat, max_lon, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Backwards", 51.51, -0.13, 51.50, -0.12, int(time.time())),
        )


def test_migrating_twice_is_a_no_op(tmp_path):
    """init_db is called on every process start, so it must be idempotent."""
    path = str(tmp_path / "twice.db")
    db.init_db(path)
    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
