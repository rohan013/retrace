#!/usr/bin/env bash
# Deploy and start the retrace macOS agent as a LaunchAgent. Safe to
# re-run: re-deploys the current source and reloads the LaunchAgent, so an
# edited agent.py or plist gets picked up.
#
#   macos/install.sh
#
# Deploys to ~/Library/Application Support/retrace-agent/ rather than
# running in place from this git checkout: macOS blocks launchd-spawned
# processes from reading files under ~/Desktop even with normal file
# permissions (TCC's Desktop/Documents/Downloads protection only extends
# consent to apps a human explicitly approved, e.g. Terminal.app) --
# confirmed by hand, the daemon fatal-errors on `pyvenv.cfg` under
# ~/Desktop/... when launchd starts it. Application Support is the
# standard per-user location for a background helper's runtime files and
# isn't covered by that protection.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$HOME/Library/Application Support/retrace-agent"
PLIST_NAME="com.rohan.retrace-agent.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "== checking prerequisites =="

if [[ ! -f "$SRC/.env" ]]; then
    echo "error: $SRC/.env does not exist — copy .env.example to .env and fill it in first" >&2
    exit 1
fi

if grep -qE '^(INGEST_TOKEN|CF_ACCESS_CLIENT_ID|CF_ACCESS_CLIENT_SECRET)=replace-me$' "$SRC/.env"; then
    echo "error: .env still has a replace-me placeholder — fill in the real values first" >&2
    exit 1
fi

echo "== deploying source to $RUNTIME_DIR =="
mkdir -p "$RUNTIME_DIR"
cp "$SRC/agent.py" "$SRC/requirements.txt" "$SRC/.env" "$RUNTIME_DIR/"
# .env carries INGEST_TOKEN and the Cloudflare service-token secret, so it does
# not inherit whatever mode the copy happened to land on.
chmod 600 "$RUNTIME_DIR/.env"

if [[ ! -x "$RUNTIME_DIR/.venv/bin/python3" ]]; then
    echo "== creating venv =="
    python3 -m venv "$RUNTIME_DIR/.venv"
fi

echo "== installing dependencies =="
"$RUNTIME_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$RUNTIME_DIR/.venv/bin/pip" install --quiet -r "$RUNTIME_DIR/requirements.txt"

echo "== installing LaunchAgent =="
mkdir -p "$LAUNCH_AGENTS_DIR" "$HOME/Library/Logs"
launchctl unload "$LAUNCH_AGENTS_DIR/$PLIST_NAME" 2>/dev/null || true
sed -e "s#__RUNTIME_DIR__#$RUNTIME_DIR#g" -e "s#__HOME__#$HOME#g" \
    "$SRC/$PLIST_NAME" > "$LAUNCH_AGENTS_DIR/$PLIST_NAME"
launchctl load "$LAUNCH_AGENTS_DIR/$PLIST_NAME"

echo
echo "== status =="
launchctl list | grep retrace-agent || echo "warning: not showing in launchctl list yet"

echo
echo "done. tail logs with: tail -f ~/Library/Logs/retrace-agent.log"
