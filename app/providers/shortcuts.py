"""iOS Shortcuts adapter.

A personal automation POSTs one flat JSON object per signal — app opened/
closed, Wi-Fi connected/disconnected, CarPlay connected/disconnected, or any
other free-text `kind`, which the events table's schemaless design accepts
directly; a new kind only needs a label added to the frontend's icon/label
table.

    {"kind": "app", "subject": "Spotify", "value": "open", "device": "iphone"}

`detect()` is keyed on the presence of `kind` alongside the absence of
OwnTracks' `_type` and Generic's `points` fields. The real automation should
still pin `?format=shortcuts` explicitly on its URL — see README — since a
failed detection fails silently from the Shortcuts side.
"""

import json
import time
from typing import Any

from .base import IngestResult, ParsedEvent, ParseResult, coerce_int

NAME = "shortcuts"
DEFAULT_DEVICE = "unknown"


class ShortcutsProvider:
    name = NAME

    def detect(self, payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and "kind" in payload
            and "_type" not in payload
            and "points" not in payload
        )

    def parse(self, payload: Any, headers: dict[str, str]) -> ParseResult:
        result = ParseResult()
        if not isinstance(payload, dict):
            return result

        kind = payload.get("kind")
        if not kind:
            return result

        # Receive-time stands in for a real timestamp — accurate enough for a
        # device-usage log, and keeps the automation to a single "Get Contents
        # of URL" action.
        ts = coerce_int(payload.get("ts")) or int(time.time())
        device = str(payload.get("device") or headers.get("x-device") or DEFAULT_DEVICE)

        result.events.append(
            ParsedEvent(
                ts=ts,
                kind=str(kind),
                source=NAME,
                subject=payload.get("subject"),
                value_text=payload.get("value"),
                device=device,
                payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )
        )
        return result

    def response(self, result: IngestResult) -> Any:
        return {
            "accepted": result.accepted,
            "duplicates": result.duplicates,
            "events": result.events,
        }
