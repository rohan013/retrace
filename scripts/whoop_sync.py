#!/usr/bin/env python3
"""Pull nightly sleep from WHOOP and post it into retrace as start/end events.

WHOOP requires a one-time interactive OAuth authorization; everything after
that is unattended. Run once by hand:

    scripts/whoop_sync.py auth

then leave the default command to the timer:

    scripts/whoop_sync.py

The server this runs against is usually reached over SSH, and the browser
that completes the authorization runs on your own machine, not the server --
so `localhost:8421` in that browser has to resolve to the server's loopback
for the redirect to land. Forward the port first:

    ssh -L 8421:localhost:8421 <host>

Deliberately stdlib-only, matching scripts/backup.py: `urllib.request` for
both the OAuth exchange and the sleep API, no dependency on `requests`.

No sync cursor: every run re-fetches a rolling LOOKBACK_DAYS window and
re-emits start/end pings for every scored sleep in it. `events_dedup`'s
unique index makes re-emission a no-op, so nothing needs to remember what was
already sent -- except the refresh token itself, which WHOOP rotates on every
use and which this script re-saves before doing anything else with it, so a
crash mid-run can never strand a token that already stopped working.
"""

import argparse
import http.server
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402  -- needs ROOT on sys.path first

TOKEN_PATH = config.BASE_DIR / "data" / "whoop_token.json"
SERVER_URL = f"http://127.0.0.1:{config.PORT}/api/v1/locations"

AUTHORIZE_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
SLEEP_URL = "https://api.prod.whoop.com/developer/v2/activity/sleep"
SCOPES = "offline read:sleep"

AUTH_PORT = 8421
AUTH_CALLBACK_PATH = "/callback"

# WHOOP's API sits behind Cloudflare, which 403s (error code 1010) on
# urllib's default `Python-urllib/x.y` User-Agent as bot traffic.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

LOOKBACK_DAYS = 3
SLEEP_PAGE_LIMIT = 25


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches exactly the one redirect WHOOP sends after the user approves."""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>Authorized. You can close this tab.</p>")

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep stdout to this script's own summary lines


def _post_form(url: str, data: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def _save_tokens(tokens: dict) -> None:
    """Persist immediately after any exchange or refresh -- see module docstring."""
    data = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": int(datetime.now(timezone.utc).timestamp()) + int(tokens.get("expires_in", 0)),
    }
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    TOKEN_PATH.write_text(json.dumps(data))
    TOKEN_PATH.chmod(0o600)


def _load_tokens() -> dict:
    if not TOKEN_PATH.exists():
        sys.exit(f"no token file at {TOKEN_PATH} -- run 'whoop_sync.py auth' first")
    return json.loads(TOKEN_PATH.read_text())


def run_auth(client_id: str, client_secret: str) -> None:
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{AUTH_PORT}{AUTH_CALLBACK_PATH}"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    print(f"If {ROOT.name}'s server is remote, first tunnel the callback port:")
    print(f"    ssh -L {AUTH_PORT}:localhost:{AUTH_PORT} <host>\n")
    print("Then open this URL and approve access:\n")
    print(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}\n")
    print(f"Waiting for the redirect on port {AUTH_PORT}...")

    server = http.server.HTTPServer(("127.0.0.1", AUTH_PORT), _CallbackHandler)
    server.result = None  # type: ignore[attr-defined]
    server.handle_request()  # blocks for exactly one request, then returns
    result = server.result  # type: ignore[attr-defined]

    if not result or "code" not in result:
        error = (result or {}).get("error", "no callback received")
        sys.exit(f"authorization failed: {error}")
    if result.get("state") != state:
        sys.exit("state mismatch on the callback -- possible CSRF, aborting")

    tokens = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    _save_tokens(tokens)
    print(f"Saved tokens to {TOKEN_PATH}")


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


def _fetch_sleep(access_token: str, start: datetime) -> list[dict]:
    records = []
    next_token = None
    while True:
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "limit": SLEEP_PAGE_LIMIT,
        }
        if next_token:
            params["nextToken"] = next_token
        request = urllib.request.Request(f"{SLEEP_URL}?{urllib.parse.urlencode(params)}")
        request.add_header("Authorization", f"Bearer {access_token}")
        request.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(request) as response:
            body = json.loads(response.read())
        records.extend(body.get("records", []))
        next_token = body.get("next_token")
        if not next_token:
            return records


def _parse_iso(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _post_event(ingest_token: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{SERVER_URL}?format=shortcuts", data=body, method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {ingest_token}")
    with urllib.request.urlopen(request):
        pass


def run_sync(client_id: str, client_secret: str, ingest_token: str, lookback_days: int) -> None:
    tokens = _load_tokens()
    tokens = _refresh(client_id, client_secret, tokens["refresh_token"])
    _save_tokens(tokens)  # before touching WHOOP's data API -- see module docstring

    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    records = _fetch_sleep(tokens["access_token"], start)

    nights = 0
    pushed = 0
    for record in records:
        if record.get("nap") or record.get("score_state") != "SCORED":
            continue
        nights += 1
        for ts, value in (
            (_parse_iso(record["start"]), "start"),
            (_parse_iso(record["end"]), "end"),
        ):
            _post_event(
                ingest_token,
                {
                    "kind": "sleep",
                    "value": value,
                    "device": "whoop",
                    "ts": ts,
                    "whoop_sleep_id": record["id"],
                },
            )
            pushed += 1

    print(f"whoop sync: {nights} nights seen, {pushed} events pushed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["auth", "sync"],
        help="'auth' for the one-time browser authorization, 'sync' (default) to fetch and push recent sleep",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=LOOKBACK_DAYS,
        help=f"how many days back to fetch sleep for (default {LOOKBACK_DAYS})",
    )
    args = parser.parse_args()

    client_id = os.environ.get("WHOOP_CLIENT_ID")
    client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not set -- fill them in .env first", file=sys.stderr)
        return 1

    if args.command == "auth":
        run_auth(client_id, client_secret)
        return 0

    ingest_token = os.environ.get("INGEST_TOKEN")
    if not ingest_token:
        print("INGEST_TOKEN not set -- fill it in .env first", file=sys.stderr)
        return 1

    try:
        run_sync(client_id, client_secret, ingest_token, args.lookback_days)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"{exc.geturl()} -> {exc.code}: {detail}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
