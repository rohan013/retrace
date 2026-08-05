"""Schema creation."""

import sqlite3
import time

import pytest

from app import db

from .conftest import TABLES, table_names


def test_a_fresh_database_gets_every_table(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path)

    assert table_names(sqlite3.connect(path)) == TABLES


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


def test_initialising_twice_leaves_the_existing_data_alone(tmp_path):
    """init_db is called on every process start, so it must be idempotent."""
    path = str(tmp_path / "twice.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO areas (name, min_lat, min_lon, max_lat, max_lon, created_at) "
        "VALUES ('Home', 51.49, -0.11, 51.51, -0.09, 0)"
    )
    conn.commit()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert table_names(conn) == TABLES
    assert conn.execute("SELECT name FROM areas").fetchone()[0] == "Home"
