"""FastAPI application.

The API is RESTful and provider-neutral: no recorder's name appears in a path, so
swapping the phone app never means reconfiguring the phone's URL. Which recorder
sent a payload is detected from its shape.

Only the ingest route is authenticated here. Everything else sits behind
Cloudflare Access with a human login policy. Ingest can't use that — a phone has
no way to complete an SSO login — so it sits behind a separate Access
application with a Service Auth policy instead: a machine credential Cloudflare
checks at the edge in place of an identity.

That's also why `/api/v1/locations` accepts POST and nothing else, with raw
fixes read back from `/api/v1/points` instead: the Service Auth credential is
meant to travel with write-only clients (a phone, an automation), so keeping the
path write-only means a leaked or shared one can never be used to read the
location history back out.
"""

import asyncio
import contextlib
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, geo, ingest, places, segment, timeline
from .auth import require_ingest_token

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# How often the background sweep looks for work. Ingest only records that a range
# is stale; the actual rebuild happens here so a phone upload returns immediately.
SWEEP_INTERVAL_SECONDS = 60


async def _sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(sweep_once)
        except Exception:  # a failed sweep must not kill the loop
            pass


def sweep_once() -> segment.RebuildResult | None:
    """Rebuild whatever ingest has marked stale."""
    with db.connection() as conn:
        dirty_from = ingest.take_dirty_from(conn)
        if dirty_from is None:
            return None
        return segment.rebuild(conn, from_ts=dirty_from)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(_sweep_forever())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="retrace", version="0.1.0", lifespan=lifespan)


def get_conn() -> Iterator[sqlite3.Connection]:
    with db.connection() as conn:
        yield conn


# -- ingest -----------------------------------------------------------------


@app.post("/api/v1/locations")
async def post_locations(
    request: Request,
    format: str | None = Query(default=None, description="Override payload detection"),
    conn: sqlite3.Connection = Depends(get_conn),
    _: None = Depends(require_ingest_token),
) -> Any:
    """Ingest location fixes.

    Always answers 200. The endpoint is an idempotent upsert, and OwnTracks treats
    anything else as a failure worth retrying.
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
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    result = ingest.store(conn, parsed)
    return JSONResponse(status_code=status.HTTP_200_OK, content=provider.response(result))


# -- queries ----------------------------------------------------------------


@app.get("/api/v1/points")
def get_points(
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    device: str | None = None,
    since_id: int = 0,
    limit: int = Query(default=5000, le=50_000),
    include_flagged: bool = True,
    simplify_m: float = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Raw fixes, exactly as recorded.

    Paginated by id rather than offset: an OFFSET scan re-reads every skipped row
    and degrades badly once a range runs to hundreds of pages.
    """
    where = ["id > ?"]
    params: list[Any] = [since_id]
    if from_ts is not None:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where.append("ts < ?")
        params.append(to_ts)
    if device:
        where.append("device = ?")
        params.append(device)
    if not include_flagged:
        where.append("anomaly = 0")
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT id, device, ts, lat, lon, accuracy, altitude, speed_mps, heading,
               battery, battery_status, connection, trigger_type, pressure,
               anomaly, anomaly_reason, source
        FROM points
        WHERE {' AND '.join(where)}
        ORDER BY id
        LIMIT ?
        """,
        params,
    ).fetchall()

    points = [dict(row) for row in rows]

    if simplify_m > 0 and len(points) > 2:
        kept = set(
            map(
                tuple,
                geo.douglas_peucker([(p["lat"], p["lon"]) for p in points], simplify_m),
            )
        )
        points = [p for p in points if (p["lat"], p["lon"]) in kept]

    return {
        "points": points,
        "next_since_id": rows[-1]["id"] if rows else since_id,
        "complete": len(rows) < limit,
    }


@app.get("/api/v1/days/{day}")
def get_day(
    day: str,
    device: str | None = None,
    tz: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        return timeline.assemble_day(conn, day, device=device, tz_name=tz)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")


@app.get("/api/v1/stays")
def get_stays(
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    device: str | None = None,
    min_confidence: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    where = ["s.confidence >= ?"]
    params: list[Any] = [min_confidence]
    if from_ts is not None:
        where.append("s.end_ts > ?")
        params.append(from_ts)
    if to_ts is not None:
        where.append("s.start_ts < ?")
        params.append(to_ts)
    if device:
        where.append("s.device = ?")
        params.append(device)

    rows = conn.execute(
        f"""
        SELECT s.*, p.name AS place_name, a.name AS area_name
        FROM stays s
        LEFT JOIN places p ON p.id = s.place_id
        LEFT JOIN areas  a ON a.id = s.area_id
        WHERE {' AND '.join(where)}
        ORDER BY s.start_ts
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/v1/stays/{stay_id}")
def patch_stay(
    stay_id: int,
    body: dict = Body(default_factory=dict),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Name the place a stay happened at, or attach a note.

    Naming creates or reuses a place and links every other stay nearby, so
    somewhere only has to be named once. Both edits live outside the derived
    layer and survive any rebuild.
    """
    import time

    stay = conn.execute("SELECT * FROM stays WHERE id = ?", (stay_id,)).fetchone()
    if stay is None:
        raise HTTPException(status_code=404, detail="No such stay")

    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")

        existing = places.find_place(conn, stay["center_lat"], stay["center_lon"])
        if existing is not None:
            places.rename_place(conn, existing["id"], name)
            place_id = existing["id"]
        else:
            place_id = places.create_place(
                conn, name, stay["center_lat"], stay["center_lon"], radius_m=stay["radius_m"]
            )
        conn.execute("UPDATE stays SET place_id = ? WHERE id = ?", (place_id, stay_id))
        places.attach_nearby_stays(conn, place_id)

    if "note" in body:
        now = int(time.time())
        anchor = ((stay["start_ts"] + stay["end_ts"]) // 2 // 300) * 300
        conn.execute(
            """
            INSERT INTO stay_notes (device, anchor_ts, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device, anchor_ts)
            DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at
            """,
            (stay["device"], anchor, body["note"], now, now),
        )

    return dict(conn.execute("SELECT * FROM stays WHERE id = ?", (stay_id,)).fetchone())


@app.get("/api/v1/trips")
def get_trips(
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    device: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if from_ts is not None:
        where.append("end_ts > ?")
        params.append(from_ts)
    if to_ts is not None:
        where.append("start_ts < ?")
        params.append(to_ts)
    if device:
        where.append("device = ?")
        params.append(device)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    rows = conn.execute(f"SELECT * FROM trips{clause} ORDER BY start_ts", params).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/events")
def get_events(
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    device: str | None = None,
    kind: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Raw event pings, unpaired. See `/api/v1/days/{day}` for the paired,
    day-assembled view used by the UI."""
    where: list[str] = []
    params: list[Any] = []
    if from_ts is not None:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where.append("ts < ?")
        params.append(to_ts)
    if device:
        where.append("device = ?")
        params.append(device)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    rows = conn.execute(f"SELECT * FROM events{clause} ORDER BY ts", params).fetchall()
    return [dict(row) for row in rows]


# -- places and areas -------------------------------------------------------


@app.get("/api/v1/places")
def get_places(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.*, COUNT(s.id) AS visit_count, COALESCE(SUM(s.end_ts - s.start_ts), 0) AS total_seconds
        FROM places p
        LEFT JOIN stays s ON s.place_id = p.id
        GROUP BY p.id
        ORDER BY total_seconds DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/v1/places", status_code=201)
def post_place(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    for field in ("name", "lat", "lon"):
        if field not in body:
            raise HTTPException(status_code=400, detail=f"'{field}' is required")

    place_id = places.create_place(
        conn,
        str(body["name"]),
        float(body["lat"]),
        float(body["lon"]),
        radius_m=body.get("radius_m"),
        category=body.get("category"),
    )
    attached = places.attach_nearby_stays(conn, place_id)
    row = dict(conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone())
    row["attached_stays"] = attached
    return row


@app.patch("/api/v1/places/{place_id}")
def patch_place(
    place_id: int,
    body: dict = Body(default_factory=dict),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM places WHERE id = ?", (place_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="No such place")

    if "name" in body:
        places.rename_place(conn, place_id, str(body["name"]))
    if "note" in body:
        conn.execute("UPDATE places SET note = ? WHERE id = ?", (body["note"], place_id))
    if "category" in body:
        conn.execute("UPDATE places SET category = ? WHERE id = ?", (body["category"], place_id))

    return dict(conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone())


@app.delete("/api/v1/places/{place_id}", status_code=204)
def delete_place(place_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> None:
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))


@app.get("/api/v1/areas")
def get_areas(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM areas ORDER BY name")]


@app.post("/api/v1/areas", status_code=201)
def post_area(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """Create a hand-drawn box area. Checked before any place lookup or
    geocoding, so this is the cheapest and least ambiguous way to name where
    you are.
    """
    import time

    for field in ("name", "min_lat", "min_lon", "max_lat", "max_lon"):
        if field not in body:
            raise HTTPException(status_code=400, detail=f"'{field}' is required")

    min_lat, min_lon = float(body["min_lat"]), float(body["min_lon"])
    max_lat, max_lon = float(body["max_lat"]), float(body["max_lon"])
    if min_lat >= max_lat or min_lon >= max_lon:
        raise HTTPException(
            status_code=400, detail="min_lat/min_lon must be less than max_lat/max_lon"
        )

    cursor = conn.execute(
        "INSERT INTO areas (name, min_lat, min_lon, max_lat, max_lon, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(body["name"]), min_lat, min_lon, max_lat, max_lon, int(time.time())),
    )

    return dict(
        conn.execute("SELECT * FROM areas WHERE id = ?", (cursor.lastrowid,)).fetchone()
    )


@app.delete("/api/v1/areas/{area_id}", status_code=204)
def delete_area(area_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> None:
    conn.execute("DELETE FROM areas WHERE id = ?", (area_id,))


# -- operations -------------------------------------------------------------


@app.get("/api/v1/devices")
def get_devices(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    # Unioned with events so a device that never sends a GPS fix -- a MacBook
    # posting only session/focus/site activity, say -- still shows up here
    # and in the frontend's device filter.
    rows = conn.execute(
        """
        SELECT device,
               SUM(points) AS points,
               SUM(events) AS events,
               MIN(first_ts) AS first_ts,
               MAX(last_ts) AS last_ts
        FROM (
            SELECT device, COUNT(*) AS points, 0 AS events,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM points GROUP BY device
            UNION ALL
            SELECT device, 0 AS points, COUNT(*) AS events,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM events WHERE device IS NOT NULL GROUP BY device
        )
        GROUP BY device
        ORDER BY last_ts DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/stats")
def get_stats(
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    device: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    return timeline.stats(conn, from_ts=from_ts, to_ts=to_ts, device=device)


@app.post("/api/v1/reprocess")
def post_reprocess(
    from_ts: int | None = Query(default=None, alias="from"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Rebuild the derived layer. Omit `from` to rebuild everything.

    The escape hatch for threshold tuning: change a value in .env, rebuild, and
    compare. Raw fixes are untouched and user edits survive.
    """
    result = segment.rebuild(conn, from_ts=from_ts)
    return {
        "window_start": result.window_start,
        "stays": result.stays,
        "trips": result.trips,
        "flagged": result.flagged,
        "devices": result.devices,
    }


@app.get("/healthz")
def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = conn.execute("SELECT COUNT(*) AS n, MAX(ts) AS latest FROM points").fetchone()
    return {"status": "ok", "points": row["n"], "latest_point_ts": row["latest"]}


# -- UI ---------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE_HTML)


# The UI is plain files with no build step, so there are no content-hashed
# filenames to bust a cache with. Without an explicit Cache-Control, browsers
# fall back to heuristic caching and will happily serve a stale app.js for
# hours — editing a file then seeing no change is a cache problem, not a code
# one, and it is worth spending a conditional request per load to never have to
# diagnose that again.
#
# `no-cache` does not mean "do not cache": it means "revalidate before reusing".
# StaticFiles already sends ETag and Last-Modified, so a revalidation is a 304
# with no body — cheap on a LAN and through the tunnel alike.
NO_CACHE = {"Cache-Control": "no-cache"}
NO_STORE_HTML = {"Cache-Control": "no-cache, must-revalidate"}


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that asks the client to revalidate instead of guessing."""

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


if STATIC_DIR.is_dir():
    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
