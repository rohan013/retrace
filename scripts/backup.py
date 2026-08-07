#!/usr/bin/env python3
"""Back up the tracker database, keeping 7 daily and 4 weekly copies.

Uses SQLite's online backup API rather than copying the file. The database runs
in WAL mode and is written to while this runs, so a byte-for-byte copy can catch
a torn page and a stale `-wal` alongside it — producing an archive that only
turns out to be unreadable on the day you need it.

Deliberately stdlib-only: `sqlite3.Connection.backup()` is the same online backup
the sqlite3 CLI performs, so the backup does not depend on an apt package being
present, and it runs under the same interpreter as the app.

Run by retrace-backup.timer. Safe to run by hand at any time.

    scripts/backup.py [--db data/tracker.db] [--dest data/backups]
"""

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP_DAILY = 7
KEEP_WEEKLY = 4


def verify(path: Path) -> int:
    """Open a snapshot and check it. Returns the number of points it holds.

    A backup that has never been opened is a guess, not a backup — so this runs
    before the archive is allowed to displace an older one. Any failure to read
    it at all counts as a failed check, not just a failed PRAGMA.
    """
    try:
        check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"integrity check failed: {result}")
            return check.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        finally:
            check.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"integrity check failed: snapshot is unreadable ({exc})") from exc


def snapshot(db: Path, target: Path) -> int:
    """Copy a live database consistently. Returns the number of points captured."""
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    return verify(target)


def prune(directory: Path, keep: int) -> None:
    # Names are date-stamped, so sorting by name sorts by date.
    archives = sorted(directory.glob("tracker-*.db.gz"), reverse=True)
    for old in archives[keep:]:
        old.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "tracker.db")
    parser.add_argument("--dest", type=Path, default=ROOT / "data" / "backups")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db} — nothing to back up", file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    daily = args.dest / "daily"
    weekly = args.dest / "weekly"
    # An archive is a complete copy of the location history, so the modes are
    # set explicitly here: this runs both under the timer, where
    # UMask=0077 in retrace-backup.service governs them, and by hand from a
    # shell whose umask is usually 002, and only the owner should ever read it.
    daily.mkdir(parents=True, exist_ok=True, mode=0o700)
    weekly.mkdir(parents=True, exist_ok=True, mode=0o700)

    archive = daily / f"tracker-{now:%Y%m%d}.db.gz"

    with tempfile.TemporaryDirectory(dir=args.dest) as tmp:
        raw = Path(tmp) / "snapshot.db"
        points = snapshot(args.db, raw)
        # Compress into place only once the snapshot has passed its check, so a
        # failed run never leaves a half-written archive where a good one was.
        with raw.open("rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        archive.chmod(0o600)

    if now.isoweekday() == 7:  # Sunday's copy is also the weekly one
        shutil.copy2(archive, weekly / archive.name)

    prune(daily, KEEP_DAILY)
    prune(weekly, KEEP_WEEKLY)

    size_mb = archive.stat().st_size / 1_048_576
    print(f"backed up {points:,} points to {archive} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
