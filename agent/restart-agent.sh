#!/usr/bin/env bash
set -u

AGENT="/mnt/cache/appdata/mos-tools/agent/agent.py"
SOCKET="/run/mos-tools/agent.sock"
LOG="/mnt/cache/appdata/mos-tools/logs/agent.log"
OLD_PID="${1:-}"

mkdir -p "$(dirname "$LOG")"

# Give the old agent enough time to return the JSON response to WebUI.
sleep 1

if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true

    # Agent has no SIGTERM handler, so allow a short clean exit window.
    for _ in $(seq 1 30); do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 0.1
    done

    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
fi

# Only remove a stale socket after the old process is gone.
rm -f "$SOCKET"

# Do not start a duplicate if another agent was started externally meanwhile.
if pgrep -f "^python3 ${AGENT}$" >/dev/null 2>&1; then
    exit 0
fi

nohup python3 "$AGENT" >>"$LOG" 2>&1 </dev/null &
exit 0
