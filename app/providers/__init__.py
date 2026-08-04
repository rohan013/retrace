"""Provider registry.

Detection is by payload shape, so the ingest URL stays provider-neutral and the
phone never needs reconfiguring if the recorder changes. `?format=` overrides
detection when a payload is ambiguous.
"""

from typing import Any

from .base import IngestResult, ParsedEvent, ParsedPoint, ParseResult, Provider
from .generic import GenericProvider
from .owntracks import OwnTracksProvider

# Order matters: the most specific detector wins.
PROVIDERS: list[Provider] = [OwnTracksProvider(), GenericProvider()]

_BY_NAME = {p.name: p for p in PROVIDERS}


def detect(payload: Any) -> Provider | None:
    """The first provider that recognises this payload, if any."""
    for provider in PROVIDERS:
        if provider.detect(payload):
            return provider
    return None


def get(name: str) -> Provider | None:
    return _BY_NAME.get(name)


def names() -> list[str]:
    return list(_BY_NAME)


__all__ = [
    "IngestResult",
    "ParsedEvent",
    "ParsedPoint",
    "ParseResult",
    "Provider",
    "PROVIDERS",
    "detect",
    "get",
    "names",
]
