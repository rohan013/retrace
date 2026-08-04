"""FastAPI application.

The API is RESTful and provider-neutral: no recorder's name appears in a path,
so swapping the phone app never means reconfiguring the phone's URL. Which
recorder sent a payload is detected from its shape.
"""

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse

from . import config, db, ingest
from .auth import require_ingest_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="tracker", version="0.1.0", lifespan=lifespan)


def get_conn() -> Iterator[sqlite3.Connection]:
    with db.connection() as conn:
        yield conn


@app.get("/healthz")
def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    points = conn.execute("SELECT COUNT(*) AS n FROM points").fetchone()["n"]
    return {"status": "ok", "points": points}


@app.post("/api/v1/locations")
async def post_locations(
    request: Request,
    format: str | None = Query(default=None, description="Override payload detection"),
    conn: sqlite3.Connection = Depends(get_conn),
    _: None = Depends(require_ingest_token),
) -> Any:
    """Ingest location fixes.

    Always answers 200. The endpoint is an idempotent upsert, and OwnTracks
    treats anything else as a failure worth retrying.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Body is not valid JSON"}
        )

    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        provider, parsed = ingest.parse_payload(payload, headers, format)
    except ingest.UnknownPayload as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
        )

    result = ingest.store(conn, parsed)
    return JSONResponse(status_code=status.HTTP_200_OK, content=provider.response(result))


@app.get("/api/v1/devices")
def get_devices(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT device,
               COUNT(*)     AS points,
               MIN(ts)      AS first_ts,
               MAX(ts)      AS last_ts
        FROM points
        GROUP BY device
        ORDER BY last_ts DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]
