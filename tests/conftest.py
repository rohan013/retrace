"""Shared fixtures.

The centrepiece is `Track`, a builder for deterministic synthetic point streams.
Segmentation thresholds are the kind of thing that gets tuned repeatedly against
real data, and a builder that can express "sat still for six hours, then drove
40 km, then the phone went quiet for two hours" makes those changes measurable
instead of a matter of opinion.
"""

import math
import random
import sqlite3
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app import config, db

TEST_TOKEN = "test-token-not-a-real-secret"

# 2026-06-01 09:00:00 UTC. Fixed so failures are reproducible.
BASE_TS = 1780304400

# Central London, arbitrary but real enough that timezone lookups behave.
HOME = (51.5074, -0.1278)


def offset_m(origin: tuple[float, float], north_m: float, east_m: float) -> tuple[float, float]:
    """Shift a coordinate by a distance in metres."""
    lat, lon = origin
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


class Track:
    """Builds a stream of points in the generic ingest format.

    Every method advances an internal clock and position, so scenarios read in
    chronological order:

        Track().stay(hours=2).drive_to(OFFICE, speed_mps=15).stay(hours=8)
    """

    def __init__(
        self,
        device: str = "phone",
        start_ts: int = BASE_TS,
        origin: tuple[float, float] = HOME,
        seed: int = 1234,
    ) -> None:
        self.device = device
        self.ts = start_ts
        self.pos = origin
        self._points: list[dict[str, Any]] = []
        self._rng = random.Random(seed)

    # -- building blocks ----------------------------------------------------

    def _emit(
        self,
        pos: tuple[float, float],
        accuracy: float = 10.0,
        speed_mps: float | None = None,
        **extra: Any,
    ) -> None:
        self._points.append(
            {
                "device": self.device,
                "ts": self.ts,
                "lat": pos[0],
                "lon": pos[1],
                "accuracy": accuracy,
                "speed_mps": speed_mps,
                **extra,
            }
        )

    def stay(
        self,
        seconds: int = 0,
        *,
        hours: float = 0,
        minutes: float = 0,
        interval: int = 60,
        jitter_m: float = 8.0,
        accuracy: float = 10.0,
    ) -> "Track":
        """Sit still, emitting jittered fixes. Jitter models real GPS wander."""
        duration = int(seconds + minutes * 60 + hours * 3600)
        centre = self.pos
        elapsed = 0
        while elapsed <= duration:
            jittered = offset_m(
                centre,
                self._rng.uniform(-jitter_m, jitter_m),
                self._rng.uniform(-jitter_m, jitter_m),
            )
            self._emit(jittered, accuracy=accuracy, speed_mps=0.0)
            self.ts += interval
            elapsed += interval
        self.ts -= interval
        self.pos = centre
        return self

    def move_to(
        self,
        destination: tuple[float, float],
        *,
        speed_mps: float = 1.4,
        interval: int = 30,
        accuracy: float = 10.0,
    ) -> "Track":
        """Travel in a straight line at a constant speed."""
        from app.geo import distance_m

        start = self.pos
        total = distance_m(start, destination)
        if total == 0:
            return self
        duration = total / speed_mps
        steps = max(1, int(duration // interval))

        for step in range(1, steps + 1):
            fraction = step / steps
            pos = (
                start[0] + (destination[0] - start[0]) * fraction,
                start[1] + (destination[1] - start[1]) * fraction,
            )
            self.ts += interval
            self._emit(pos, accuracy=accuracy, speed_mps=speed_mps)

        self.pos = destination
        return self

    def gap(self, seconds: int = 0, *, hours: float = 0, minutes: float = 0) -> "Track":
        """Advance time without emitting anything — the phone went quiet."""
        self.ts += int(seconds + minutes * 60 + hours * 3600)
        return self

    def outlier(self, north_m: float = 50_000, east_m: float = 0.0) -> "Track":
        """A single wild fix that jumps away and back.

        This is the classic stale cell or wifi fix: its neighbours are sane, and
        the round trip to reach it is physically impossible.
        """
        self.ts += 30
        self._emit(offset_m(self.pos, north_m, east_m), accuracy=1500.0)
        self.ts += 30
        return self

    def point(self, **overrides: Any) -> "Track":
        """A single fix at the current position, for one-off cases."""
        self._emit(self.pos, **overrides)
        return self

    # -- output -------------------------------------------------------------

    def points(self) -> list[dict[str, Any]]:
        return list(self._points)

    def payload(self) -> dict[str, Any]:
        return {"points": self._points}

    def insert(self, conn: sqlite3.Connection) -> None:
        """Write straight to the database, bypassing HTTP."""
        from app import ingest
        from app.providers.generic import GenericProvider

        parsed = GenericProvider().parse(self.payload(), {})
        ingest.store(conn, parsed)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "INGEST_TOKEN", TEST_TOKEN)
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path: str) -> Iterator[sqlite3.Connection]:
    with db.connection(db_path) as connection:
        yield connection


@pytest.fixture
def client(db_path: str) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        yield test_client


@pytest.fixture
def anon_client(db_path: str) -> Iterator[TestClient]:
    """A client with no credentials, for auth tests."""
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
