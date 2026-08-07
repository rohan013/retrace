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


def _hostname(name: str, default: str) -> str:
    """A bare hostname, however it was written down.

    Compared against the Host header, which carries no scheme, path or port. A
    full URL is the natural thing to paste into the .env, and would match
    nothing -- locking the tunnel out with a 400 -- so those are stripped.
    """
    value = _str(name, default).strip()
    if "//" in value:
        value = value.split("//", 1)[1]
    return value.split("/", 1)[0].split(":", 1)[0]


INGEST_TOKEN = _str("INGEST_TOKEN", "")

# Largest ingest body accepted. `request.json()` buffers the whole thing before
# parsing, so without a bound a single request decides how much memory this
# process uses. Real payloads are a few hundred bytes each and a batch is a few
# tens of KB, so this leaves several orders of magnitude of headroom.
MAX_INGEST_BYTES = _int("MAX_INGEST_BYTES", 2 * 1024 * 1024)

HOST = _str("HOST", "127.0.0.1")
PORT = _int("PORT", 8420)

# The hostname the tunnel routes to this service, e.g. tracker.example.com.
# Binding to loopback keeps the network out, but a browser running on this
# machine can still be pointed here by a page that resolves its own hostname to
# 127.0.0.1 -- at which point the page is same-origin with the API and
# Cloudflare never sees the request. Naming the hostnames that are allowed to
# reach it closes that. Left empty, the check is not installed, so an unset
# value cannot lock the tunnel out.
PUBLIC_HOSTNAME = _hostname("PUBLIC_HOSTNAME", "")

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
GEOCODER_USER_AGENT = _str("GEOCODER_USER_AGENT", "retrace")
