"""Environment-driven settings.

Values are read from the process environment, seeded from a .env file at the
repo root if one exists. Tests override module attributes directly.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


INGEST_TOKEN = _str("INGEST_TOKEN", "")

HOST = _str("HOST", "127.0.0.1")
PORT = _int("PORT", 8420)

_db = _str("DB_PATH", "data/tracker.db")
DB_PATH = str(Path(_db) if Path(_db).is_absolute() else BASE_DIR / _db)

# Segmentation
STAY_RADIUS_M = _float("STAY_RADIUS_M", 70)
STAY_DRIFT_CAP = _float("STAY_DRIFT_CAP", 1.5)
STAY_MIN_SECONDS = _int("STAY_MIN_SECONDS", 300)
GAP_MAX_SECONDS = _int("GAP_MAX_SECONDS", 3600)
GAP_RESUME_DISTANCE_M = _float("GAP_RESUME_DISTANCE_M", 100)
GAP_RESUME_MAX_SECONDS = _int("GAP_RESUME_MAX_SECONDS", 12 * 3600)
MERGE_GAP_SECONDS = _int("MERGE_GAP_SECONDS", 900)

# Quality
ABSURD_ACCURACY_M = _float("ABSURD_ACCURACY_M", 10_000)
NULL_ISLAND_RADIUS_M = _float("NULL_ISLAND_RADIUS_M", 5_000)
MAX_DETOUR_SPEED_MPS = _float("MAX_DETOUR_SPEED_MPS", 83)
MAX_OUTLIER_RUN = 5

# How far back a rebuild reaches from the segmentation cursor, so that a stay
# straddling the previous window boundary is reconsidered whole.
REBUILD_LOOKBACK_SECONDS = _int("REBUILD_LOOKBACK_SECONDS", 6 * 3600)

# Places
PLACE_REUSE_RADIUS_M = _float("PLACE_REUSE_RADIUS_M", 50)
GEOCODING_ENABLED = _bool("GEOCODING_ENABLED", False)
GEOCODER_USER_AGENT = _str("GEOCODER_USER_AGENT", "personal-tracker")
