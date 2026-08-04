"""Ingest authentication.

The token can arrive three ways because recorder apps differ in what they can
set. OwnTracks iOS can send HTTP Basic credentials but not arbitrary headers;
other clients can only put a token in the URL. All three are the same secret.

The web UI is not protected here — Cloudflare Access sits in front of it. The
ingest path is deliberately excluded from Access (a phone cannot complete an SSO
login) and is protected by this token instead.
"""

import base64
import binascii
import secrets

from fastapi import HTTPException, Query, Request, status

from . import config


def _token_from_basic(header: str) -> str | None:
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (IndexError, binascii.Error, UnicodeDecodeError):
        return None
    # The username is ignored; OwnTracks sends the device user there.
    _, _, password = decoded.partition(":")
    return password or None


def extract_token(request: Request, token_param: str | None = None) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip() or None
    if authorization.lower().startswith("basic "):
        return _token_from_basic(authorization)
    return token_param


def require_ingest_token(request: Request, token: str | None = Query(default=None)) -> None:
    """FastAPI dependency guarding every ingest route."""
    expected = config.INGEST_TOKEN
    if not expected:
        # Failing closed matters more than convenience: an unset token would
        # otherwise leave a public write endpoint on the internet.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INGEST_TOKEN is not configured on the server",
        )

    supplied = extract_token(request, token)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing ingest token",
            headers={"WWW-Authenticate": 'Basic realm="tracker"'},
        )
