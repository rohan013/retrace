"""SQLite schema and connection handling.

`SCHEMA` below is the whole schema, and `init_db` runs it against a database
that has no tables yet. A schema change means editing `SCHEMA`, stopping the
service, applying the matching ALTER by hand and starting it again — see
"Changing the schema" in the README.

Design notes that the schema encodes deliberately:

* `points` is immutable and never deleted. Bad fixes are *flagged*
  (`anomaly`), never removed, because a reported accuracy radius is a
  confidence estimate rather than proof a position is wrong — dropping
  high-radius fixes replaces real route geometry with straight lines.
* Derived rows (`stays`, `trips`) are disposable: any window can be deleted and
  rebuilt from `points` alone. Nothing in `points` records which stay claimed it,
  so a stay can always grow, shrink or be re-evaluated when new data arrives.
* User edits live in `places` and `stay_notes`, which a rebuild never touches.
* All timestamps are UTC unix seconds. All durations are seconds.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE points (
    id                INTEGER PRIMARY KEY,
    device            TEXT    NOT NULL,
    ts                INTEGER NOT NULL,
    lat               REAL    NOT NULL,
    lon               REAL    NOT NULL,
    accuracy          REAL,
    altitude          REAL,
    vertical_accuracy REAL,
    speed_mps         REAL,
    heading           REAL,
    battery           REAL,
    battery_status    TEXT,
    connection        TEXT,
    trigger_type      TEXT,
    ssid              TEXT,
    bssid             TEXT,
    pressure          REAL,
    monitoring_mode   INTEGER,
    in_regions        TEXT,
    source            TEXT    NOT NULL,
    raw               TEXT,
    anomaly           INTEGER NOT NULL DEFAULT 0,
    anomaly_reason    TEXT,
    created_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX points_device_ts ON points(device, ts);
CREATE INDEX points_ts ON points(ts);

CREATE TABLE places (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    lat            REAL    NOT NULL,
    lon            REAL    NOT NULL,
    radius_m       REAL,
    address        TEXT,
    category       TEXT,
    source         TEXT    NOT NULL DEFAULT 'auto',
    name_locked_at INTEGER,
    note           TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX places_latlon ON places(lat, lon);

CREATE TABLE areas (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    min_lat    REAL    NOT NULL,
    min_lon    REAL    NOT NULL,
    max_lat    REAL    NOT NULL,
    max_lon    REAL    NOT NULL,
    created_at INTEGER NOT NULL,
    CHECK (min_lat < max_lat AND min_lon < max_lon)
);

CREATE TABLE stays (
    id                   INTEGER PRIMARY KEY,
    device               TEXT    NOT NULL,
    start_ts             INTEGER NOT NULL,
    end_ts               INTEGER NOT NULL,
    center_lat           REAL    NOT NULL,
    center_lon           REAL    NOT NULL,
    radius_m             REAL    NOT NULL,
    point_count          INTEGER NOT NULL,
    first_point_id       INTEGER,
    last_point_id        INTEGER,
    place_id             INTEGER REFERENCES places(id) ON DELETE SET NULL,
    area_id              INTEGER REFERENCES areas(id) ON DELETE SET NULL,
    tz                   TEXT,
    had_gap              INTEGER NOT NULL DEFAULT 0,
    confidence           INTEGER,
    confidence_breakdown TEXT,
    created_at           INTEGER NOT NULL
);
CREATE INDEX stays_device_start ON stays(device, start_ts);
CREATE INDEX stays_start ON stays(start_ts);

CREATE TABLE trips (
    id           INTEGER PRIMARY KEY,
    device       TEXT    NOT NULL,
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    distance_m   REAL    NOT NULL,
    point_count  INTEGER NOT NULL,
    start_lat    REAL,
    start_lon    REAL,
    end_lat      REAL,
    end_lon      REAL,
    avg_speed    REAL,
    max_speed    REAL,
    mode         TEXT,
    from_stay_id INTEGER REFERENCES stays(id) ON DELETE SET NULL,
    to_stay_id   INTEGER REFERENCES stays(id) ON DELETE SET NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX trips_device_start ON trips(device, start_ts);
CREATE INDEX trips_start ON trips(start_ts);

-- Survives rebuilds. Keyed on a stay's midpoint rounded to 5 minutes, which is
-- stable enough that re-segmentation re-attaches the note to the same stay.
CREATE TABLE stay_notes (
    id         INTEGER PRIMARY KEY,
    device     TEXT    NOT NULL,
    anchor_ts  INTEGER NOT NULL,
    note       TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX stay_notes_device_anchor ON stay_notes(device, anchor_ts);

-- The extension point for passive sources (iOS Shortcuts, geofence
-- transitions, and whatever comes later). `payload` is schemaless, so a new
-- source's ingest route can start writing rows straight away.
CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    subject    TEXT,
    device     TEXT,
    lat        REAL,
    lon        REAL,
    value_num  REAL,
    value_text TEXT,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX events_dedup ON events(source, ts, kind, IFNULL(subject, ''), IFNULL(device, ''));
CREATE INDEX events_ts ON events(ts);
CREATE INDEX events_device_ts ON events(device, ts);

CREATE TABLE state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db(path: str | None = None) -> None:
    """Create the database and its tables if they are not there yet.

    Called on every process start, so an already-populated database is left
    exactly as it is.
    """
    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = _raw_connect(str(target))
    try:
        if _is_empty(conn):
            conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row[0] == 0


def _raw_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def connection(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """A connection with WAL and foreign keys enabled."""
    conn = _raw_connect(path or config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
