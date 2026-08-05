"""OwnTracks HTTP-mode adapter.

Two quirks of this client are load-bearing and are handled here so they never
reach the API layer:

1. It requires a bare JSON array as the response body. OwnTracks reads that array
   as a list of commands to execute; returning an object makes it treat the POST
   as failed and retry.
2. `vel` is km/h. Everything downstream is m/s.

Device identity comes from the X-Limit-D header OwnTracks sets in HTTP mode,
falling back to the MQTT-style topic and then the two-character tracker id.
"""

import json
from typing import Any

from .base import ParsedEvent, ParsedPoint, ParseResult, IngestResult, coerce_float, coerce_int

NAME = "owntracks"

# bs: battery status
_BATTERY_STATUS = {0: "unknown", 1: "unplugged", 2: "charging", 3: "full"}

# conn: connectivity at the time of the fix
_CONNECTION = {"w": "wifi", "m": "mobile", "o": "offline"}

# Payload types we knowingly accept and discard. OwnTracks posts these to the
# same endpoint; they are not errors, they simply carry no location.
_IGNORED_TYPES = {"lwt", "waypoint", "waypoints", "card", "cmd", "status", "encrypted"}


class OwnTracksProvider:
    name = NAME

    def detect(self, payload: Any) -> bool:
        return isinstance(payload, dict) and "_type" in payload

    def parse(self, payload: Any, headers: dict[str, str]) -> ParseResult:
        result = ParseResult()
        if not isinstance(payload, dict):
            return result

        kind = payload.get("_type")
        device = _device_id(payload, headers)

        if kind == "location":
            point = _parse_location(payload, device)
            if point is not None:
                result.points.append(point)
        elif kind == "transition":
            event = _parse_transition(payload, device)
            if event is not None:
                result.events.append(event)
        elif kind in _IGNORED_TYPES:
            pass

        return result

    def response(self, result: IngestResult) -> Any:
        # Must be a bare array. See module docstring.
        return []


def _device_id(payload: dict, headers: dict[str, str]) -> str:
    device = headers.get("x-limit-d")
    if device:
        return device
    topic = payload.get("topic")
    if isinstance(topic, str) and topic:
        # owntracks/<user>/<device>
        return topic.rsplit("/", 1)[-1]
    tid = payload.get("tid")
    if isinstance(tid, str) and tid:
        return tid
    return "unknown"


def _parse_location(payload: dict, device: str) -> ParsedPoint | None:
    lat = coerce_float(payload.get("lat"))
    lon = coerce_float(payload.get("lon"))
    ts = coerce_int(payload.get("tst"))
    if lat is None or lon is None or ts is None:
        return None

    # vel is km/h, and -1 means "not known" rather than "reversing".
    vel = coerce_float(payload.get("vel"))
    speed_mps = vel / 3.6 if vel is not None and vel >= 0 else None

    regions = payload.get("inregions")
    in_regions = json.dumps(regions) if isinstance(regions, list) and regions else None

    return ParsedPoint(
        device=device,
        ts=ts,
        lat=lat,
        lon=lon,
        accuracy=coerce_float(payload.get("acc")),
        altitude=coerce_float(payload.get("alt")),
        vertical_accuracy=coerce_float(payload.get("vac")),
        speed_mps=speed_mps,
        heading=coerce_float(payload.get("cog")),
        battery=coerce_float(payload.get("batt")),
        battery_status=_BATTERY_STATUS.get(coerce_int(payload.get("bs"))),
        connection=_CONNECTION.get(payload.get("conn"), payload.get("conn")),
        trigger_type=payload.get("t"),
        ssid=payload.get("SSID"),
        bssid=payload.get("BSSID"),
        pressure=coerce_float(payload.get("p")),
        monitoring_mode=coerce_int(payload.get("m")),
        in_regions=in_regions,
        source=NAME,
        raw=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def _parse_transition(payload: dict, device: str) -> ParsedEvent | None:
    ts = coerce_int(payload.get("tst"))
    if ts is None:
        return None
    return ParsedEvent(
        ts=ts,
        kind="geofence",
        source=NAME,
        subject=payload.get("desc") or payload.get("t"),
        lat=coerce_float(payload.get("lat")),
        lon=coerce_float(payload.get("lon")),
        value_text=payload.get("event"),
        device=device,
        payload=json.dumps(
            {"device": device, **payload}, separators=(",", ":"), sort_keys=True
        ),
    )
