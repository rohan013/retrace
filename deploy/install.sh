#!/usr/bin/env bash
# Install and start the retrace systemd units. Safe to re-run: copying the same
# unit files and re-enabling an already-enabled service are both no-ops, and any
# unit content changes are picked up by daemon-reload + restart.
#
#   deploy/install.sh
#
# Asks for sudo up front (unit files and systemctl both need root) rather than
# partway through, so it doesn't stop mid-way waiting on a password prompt.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR=/etc/systemd/system

echo "== checking prerequisites =="

if [[ ! -f "$ROOT/.env" ]]; then
    echo "error: $ROOT/.env does not exist — copy .env.example to .env and set INGEST_TOKEN first" >&2
    exit 1
fi

if grep -q '^INGEST_TOKEN=replace-me$' "$ROOT/.env"; then
    echo "error: INGEST_TOKEN in .env is still the placeholder value" >&2
    echo "  generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
    exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/uvicorn" ]]; then
    echo "error: $ROOT/.venv/bin/uvicorn not found — run:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

install -d -m 700 "$ROOT/data"

# .env holds INGEST_TOKEN and data/ holds every fix ever recorded, so neither
# has any business being group- or world-readable. UMask=0077 in the unit
# governs what the service writes itself; this covers everything else, and
# re-asserts both every time this script runs.
echo "== tightening permissions on .env and data/ =="
chmod 600 "$ROOT/.env"
find "$ROOT/data" -type d -exec chmod 700 {} +
find "$ROOT/data" -type f -exec chmod 600 {} +

echo "== installing unit files (sudo) =="
sudo cp "$ROOT"/deploy/retrace.service "$ROOT"/deploy/retrace-backup.service \
        "$ROOT"/deploy/retrace-backup.timer "$UNIT_DIR/"
sudo systemctl daemon-reload

echo "== enabling and starting =="
sudo systemctl enable --now retrace.service retrace-backup.timer

echo
echo "== status =="
systemctl --no-pager status retrace.service
echo
systemctl list-timers retrace-backup.timer --no-pager

echo
echo "done. tail logs with: journalctl -fu retrace"
