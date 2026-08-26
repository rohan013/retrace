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

if grep -qE '^WHOOP_CLIENT_ID=.+' "$ROOT/.env" && grep -qE '^WHOOP_CLIENT_SECRET=.+' "$ROOT/.env"; then
    echo "== installing WHOOP sync timer (sudo) =="
    sudo cp "$ROOT"/deploy/retrace-whoop.service "$ROOT"/deploy/retrace-whoop.timer "$UNIT_DIR/"
    sudo systemctl daemon-reload
    sudo systemctl enable --now retrace-whoop.timer
    if [[ ! -f "$ROOT/data/whoop_token.json" ]]; then
        echo "  note: no data/whoop_token.json yet — run '.venv/bin/python scripts/whoop_sync.py auth' once by hand, or the timer's runs will fail until then"
    fi
else
    echo "== skipping WHOOP sync timer (WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not set in .env) =="
fi

if grep -qE '^ALERT_COMMAND=.+' "$ROOT/.env"; then
    ALERT_CMD="$(sed -n 's/^ALERT_COMMAND=//p' "$ROOT/.env" | head -1 | tr -d '"'"'"'')"
    echo "== installing freshness alert timer (sudo) =="
    sudo cp "$ROOT"/deploy/retrace-freshness.service "$ROOT"/deploy/retrace-freshness.timer \
            "$ROOT"/deploy/retrace-alert@.service "$UNIT_DIR/"
    sudo systemctl daemon-reload
    sudo systemctl enable --now retrace-freshness.timer
    if [[ ! -x "$ALERT_CMD" ]]; then
        echo "  warning: ALERT_COMMAND ($ALERT_CMD) is not executable — alerts will fail until it is"
    fi
    if ! grep -qE '^STALE_ALERT_DEVICE=.+' "$ROOT/.env"; then
        echo "  note: STALE_ALERT_DEVICE is empty — set it to the device to watch (see GET /api/v1/devices), or every run will exit 1"
    fi
else
    echo "== skipping freshness alert timer (ALERT_COMMAND not set in .env) =="
fi

echo
echo "== status =="
systemctl --no-pager status retrace.service
echo
systemctl list-timers retrace-backup.timer --no-pager
if systemctl is-enabled --quiet retrace-whoop.timer 2>/dev/null; then
    echo
    systemctl list-timers retrace-whoop.timer --no-pager
fi
if systemctl is-enabled --quiet retrace-freshness.timer 2>/dev/null; then
    echo
    systemctl list-timers retrace-freshness.timer --no-pager
fi

echo
echo "done. tail logs with: journalctl -fu retrace"
