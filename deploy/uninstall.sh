#!/usr/bin/env bash
# Stop and remove the tracker systemd units. Leaves the repo, .env and data/
# untouched — this only undoes what install.sh did to the system.
#
#   deploy/uninstall.sh

set -euo pipefail

UNIT_DIR=/etc/systemd/system
UNITS=(tracker.service tracker-backup.service tracker-backup.timer)

echo "== stopping =="
sudo systemctl stop "${UNITS[@]}" 2>/dev/null || true

echo "== disabling =="
sudo systemctl disable "${UNITS[@]}" 2>/dev/null || true

echo "== removing unit files =="
for unit in "${UNITS[@]}"; do
    if [[ -f "$UNIT_DIR/$unit" ]]; then
        sudo rm "$UNIT_DIR/$unit"
        echo "  removed $UNIT_DIR/$unit"
    fi
done

sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

echo
echo "done. the database, backups and .env in $(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd) are untouched."
