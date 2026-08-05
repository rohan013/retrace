"""Provider adapter contract.

A provider owns everything specific to one recorder app: how to recognise its
payload, how to read points out of it, and what shape of response it demands
back. Keeping the response quirks here is the point — OwnTracks requires a bare
JSON array, which is not REST-shaped, and that weirdness must not leak into the
public API contract. Adding a recorder is one new file plus a registry entry;
the URL the phone posts to never changes.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ParsedPoint:
    """One GPS fix, normalised across providers.

    Units are metres, seconds, m/s and degrees regardless of what the provider
    sent — OwnTracks reports velocity in km/h, for instance, and converting at
    the boundary keeps the rest of the system honest.
    """

    device: str
    ts: int
    lat: float
    lon: float
    accuracy: float | None = None
    altitude: float | None = None
    vertical_accuracy: float | None = None
    speed_mps: float | None = None
    heading: float | None = None
    battery: float | None = None
    battery_status: str | None = None
    connection: str | None = None
    trigger_type: str | None = None
    ssid: str | None = None
    bssid: str | None = None
    pressure: float | None = None
    monitoring_mode: int | None = None
    in_regions: str | None = None
    source: str = "unknown"
    raw: str | None = None


@dataclass(slots=True)
class ParsedEvent:
    """A non-GPS observation — a geofence transition, a Shortcuts ping, etc."""

    ts: int
    kind: str
    source: str
    subject: str | None = None
    lat: float | None = None
    lon: float | None = None
    value_num: float | None = None
    value_text: str | None = None
    device: str | None = None
    payload: str | None = None


@dataclass(slots=True)
class ParseResult:
    points: list[ParsedPoint] = field(default_factory=list)
    events: list[ParsedEvent] = field(default_factory=list)


@dataclass(slots=True)
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    events: int = 0


@runtime_checkable
class Provider(Protocol):
    name: str

    def detect(self, payload: Any) -> bool:
        """True if this payload belongs to this provider."""

    def parse(self, payload: Any, headers: dict[str, str]) -> ParseResult:
        """Normalise the payload. Headers carry device identity for some providers."""

    def response(self, result: IngestResult) -> Any:
        """The body this provider's client expects back."""


def coerce_float(value: Any) -> float | None:
    """Providers send numbers as strings, and sentinels for 'unknown'."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_int(value: Any) -> int | None:
    f = coerce_float(value)
    return int(f) if f is not None else None
