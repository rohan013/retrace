#!/usr/bin/env bash
# Stop and remove the retrace macOS agent's LaunchAgent. Leaves the
# deployed runtime directory (its .env, .venv, and queue.jsonl) untouched
# -- this only undoes what install.sh did to the system.
#
#   macos/uninstall.sh

set -euo pipefail

PLIST_NAME="com.rohan.retrace-agent.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
RUNTIME_DIR="$HOME/Library/Application Support/retrace-agent"

echo "== stopping and unloading =="
launchctl unload "$PLIST_PATH" 2>/dev/null || true

echo "== removing plist =="
if [[ -f "$PLIST_PATH" ]]; then
    rm "$PLIST_PATH"
    echo "  removed $PLIST_PATH"
else
    echo "  $PLIST_PATH not present, nothing to remove"
fi

echo
echo "done. the deployed runtime dir (.env, .venv, queue.jsonl) is untouched:"
echo "  $RUNTIME_DIR"
echo "remove it by hand if you want to fully clean up."
