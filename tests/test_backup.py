"""The backup script.

Worth testing because a backup is the one piece of this system whose failure is
invisible until the day it matters, and because the raw fixes are the only part
of the database that cannot be recomputed from anything else.
"""

import gzip
import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .conftest import HOME, TABLES, Track, offset_m, table_names

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_backup():
    spec = importlib.util.spec_from_file_location("backup", SCRIPTS / "backup.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup"] = module
    spec.loader.exec_module(module)
    return module


backup = _load_backup()


def test_snapshot_captures_a_database_that_is_open_and_being_written(conn, db_path, tmp_path):
    """The live database is in WAL mode with an open connection holding it.

    A plain file copy here can pick up a torn page plus a stale -wal; the online
    backup API takes a consistent snapshot instead.
    """
    Track().stay(hours=2).move_to(offset_m(HOME, 3_000, 0), speed_mps=12).stay(hours=1).insert(conn)

    target = tmp_path / "snap.db"
    points = backup.snapshot(Path(db_path), target)

    expected = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]
    assert points == expected > 0


def test_uncheckpointed_writes_are_in_the_snapshot(conn, db_path, tmp_path):
    """In WAL mode the newest rows live in the -wal file, not the database file.

    Copying only tracker.db would silently lose everything written since the last
    checkpoint — which, for a tracker, is the most recent fixes.
    """
    Track().stay(hours=2).insert(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]

    target = tmp_path / "snap.db"
    assert backup.snapshot(Path(db_path), target) == before

    restored = sqlite3.connect(target)
    assert restored.execute("SELECT MAX(ts) FROM points").fetchone()[0] == (
        conn.execute("SELECT MAX(ts) AS t FROM points").fetchone()["t"]
    )


def test_a_restored_snapshot_holds_every_table(conn, db_path, tmp_path):
    Track().stay(hours=2).insert(conn)
    conn.execute(
        "INSERT INTO areas (name, min_lat, min_lon, max_lat, max_lon, created_at) "
        "VALUES ('Home', 51.49, -0.11, 51.51, -0.09, 0)"
    )

    target = tmp_path / "snap.db"
    backup.snapshot(Path(db_path), target)

    restored = sqlite3.connect(target)
    assert restored.execute("SELECT COUNT(*) FROM points").fetchone()[0] > 0
    assert restored.execute("SELECT name FROM areas").fetchone()[0] == "Home"
    # The schema travels with the data, so a restore is ready to serve as it is.
    assert table_names(restored) == TABLES


def test_an_unreadable_snapshot_is_refused_rather_than_rotated_in(conn, db_path, tmp_path):
    """The check runs before the archive replaces anything older."""
    Track().stay(hours=1).insert(conn)
    target = tmp_path / "snap.db"
    backup.snapshot(Path(db_path), target)

    with target.open("r+b") as handle:  # scribble over the SQLite file header
        handle.write(b"\x00" * 100)

    with pytest.raises(RuntimeError, match="integrity check failed"):
        backup.verify(target)


def test_rotation_keeps_the_newest_and_drops_the_rest(tmp_path):
    for day in range(1, 11):
        (tmp_path / f"tracker-202601{day:02d}.db.gz").touch()

    backup.prune(tmp_path, 7)

    kept = sorted(p.name for p in tmp_path.iterdir())
    assert len(kept) == 7
    assert kept[0] == "tracker-20260104.db.gz"
    assert kept[-1] == "tracker-20260110.db.gz"


def test_rotation_leaves_a_short_history_alone(tmp_path):
    for day in (1, 2, 3):
        (tmp_path / f"tracker-202601{day:02d}.db.gz").touch()

    backup.prune(tmp_path, 7)

    assert len(list(tmp_path.iterdir())) == 3


def test_a_full_run_writes_a_gzipped_archive_that_opens(conn, db_path, tmp_path, monkeypatch):
    Track().stay(hours=3).insert(conn)
    dest = tmp_path / "backups"

    monkeypatch.setattr(sys, "argv", ["backup.py", "--db", db_path, "--dest", str(dest)])
    assert backup.main() == 0

    stamp = f"{datetime.now(timezone.utc):%Y%m%d}"
    archive = dest / "daily" / f"tracker-{stamp}.db.gz"
    assert archive.exists()

    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(archive.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT COUNT(*) FROM points").fetchone()[0] > 0


def test_a_missing_database_is_reported_rather_than_crashing(tmp_path, monkeypatch):
    """The timer fires on a machine where the app may never have started."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["backup.py", "--db", str(tmp_path / "nope.db"), "--dest", str(tmp_path / "b")],
    )
    assert backup.main() == 0
