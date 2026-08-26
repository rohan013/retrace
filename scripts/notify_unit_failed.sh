#!/usr/bin/env bash
# Report that a systemd unit failed, through whatever ALERT_COMMAND names.
#
# Run by retrace-alert@.service, which the other units name in OnFailure=. Kept
# as a shell script rather than folded into freshness_check.py because it must
# work when the Python side is exactly what is broken.
#
#   scripts/notify_unit_failed.sh retrace-backup.service

set -uo pipefail

UNIT="${1:-unknown unit}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# systemd runs this with no EnvironmentFile, the same as the other oneshots, so
# .env is read here. A value already in the environment wins, matching the
# setdefault semantics app/config.py uses.
if [[ -z "${ALERT_COMMAND:-}" && -r "$ROOT/.env" ]]; then
    ALERT_COMMAND="$(sed -n 's/^ALERT_COMMAND=//p' "$ROOT/.env" | head -1 | tr -d '"'"'"'')"
fi

if [[ -z "${ALERT_COMMAND:-}" ]]; then
    echo "notify_unit_failed: ALERT_COMMAND not set in $ROOT/.env" >&2
    exit 1
fi

# The last few journal lines are what actually says why it failed, and are worth
# far more on a phone than the unit name alone.
DETAIL="$(journalctl -u "$UNIT" -n 8 --no-pager -o cat 2>/dev/null || true)"

{
    echo "$UNIT failed on $(hostname)."
    [[ -n "$DETAIL" ]] && printf '\n%s\n' "$DETAIL"
} | "$ALERT_COMMAND"
