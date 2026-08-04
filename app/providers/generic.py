"""Generic adapter for scripts, tests and future recorders.

Accepts our own normalised field names, so nothing has to be translated:

    {"points": [{"device": "phone", "ts": 1754200000, "lat": 51.5, "lon": -0.12,
                 "accuracy": 8.0, "speed_mps": 1.4}]}

Unlike the OwnTracks adapter this returns a proper REST body, because no client
here demands otherwise.
"""

import json
from typing import Any

from .base import ParsedPoint, ParseResult, IngestResult, coerce_float, coerce_int

NAME = "generic"

_FIELDS = (
    "accuracy",
    "altitude",
    "vertical_accuracy",
    "speed_mps",
    "heading",
    "battery",
    "pressure",
)


class GenericProvider:
    name = NAME

    def detect(self, payload: Any) -> bool:
        return isinstance(payload, dict) and isinstance(payload.get("points"), list)

    def parse(self, payload: Any, headers: dict[str, str]) -> ParseResult:
        result = ParseResult()
        default_device = headers.get("x-device") or payload.get("device") or "unknown"

        for item in payload.get("points", []):
            if not isinstance(item, dict):
                continue
            lat = coerce_float(item.get("lat"))
            lon = coerce_float(item.get("lon"))
            ts = coerce_int(item.get("ts"))
            if lat is None or lon is None or ts is None:
                continue

            values = {name: coerce_float(item.get(name)) for name in _FIELDS}
            result.points.append(
                ParsedPoint(
                    device=str(item.get("device") or default_device),
                    ts=ts,
                    lat=lat,
                    lon=lon,
                    battery_status=item.get("battery_status"),
                    connection=item.get("connection"),
                    trigger_type=item.get("trigger_type"),
                    ssid=item.get("ssid"),
                    bssid=item.get("bssid"),
                    monitoring_mode=coerce_int(item.get("monitoring_mode")),
                    source=str(item.get("source") or NAME),
                    raw=json.dumps(item, separators=(",", ":"), sort_keys=True),
                    **values,
                )
            )
        return result

    def response(self, result: IngestResult) -> Any:
        return {
            "accepted": result.accepted,
            "duplicates": result.duplicates,
            "events": result.events,
        }
