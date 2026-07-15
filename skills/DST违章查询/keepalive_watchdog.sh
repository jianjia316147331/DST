#!/bin/bash
# Pull-mode watchdog: reads health file and restarts keepalive if stale.
# Called by systemd timer every 60s. Process-level isolation ensures this
# works even if the daemon's main thread is completely blocked.

HEALTH_FILE="$1"
SERVICE_NAME="$2"
MAX_STALE_SEC="${3:-180}"       # default: 3 min for normal state
MAX_RECOVERING_SEC="${4:-600}"  # default: 10 min for recovering state

if [ -z "$HEALTH_FILE" ] || [ -z "$SERVICE_NAME" ]; then
    echo "Usage: $0 <health_file> <service_name> [max_stale_sec] [max_recovering_sec]" >&2
    exit 1
fi

if [ ! -f "$HEALTH_FILE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Health file not found: $HEALTH_FILE — restarting $SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    exit 0
fi

NOW=$(date +%s)
LAST_CHECK=$(python3 -c "
import json, sys
try:
    with open('$HEALTH_FILE') as f:
        d = json.load(f)
    ts = d.get('last_check', '')
    from datetime import datetime
    dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    print(int(dt.timestamp()))
except Exception as e:
    print('PARSE_ERROR', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

if [ "$LAST_CHECK" = "PARSE_ERROR" ] || [ -z "$LAST_CHECK" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Health file corrupt — restarting $SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    exit 0
fi

STALE=$((NOW - LAST_CHECK))
STATE=$(python3 -c "import json; print(json.load(open('$HEALTH_FILE')).get('state','unknown'))" 2>/dev/null)

if [ "$STATE" = "recovering" ]; then
    THRESHOLD=$MAX_RECOVERING_SEC
else
    THRESHOLD=$MAX_STALE_SEC
fi

if [ "$STALE" -gt "$THRESHOLD" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Health file stale: ${STALE}s > ${THRESHOLD}s (state=$STATE) — restarting $SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] OK: ${STALE}s stale (state=$STATE, threshold=${THRESHOLD}s)"
fi
