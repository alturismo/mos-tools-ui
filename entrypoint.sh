#!/bin/sh
set -e

echo "[MOS-TOOLS] Starting scheduler..."
python /app/scheduler.py &
SCHEDULER_PID=$!

echo "[MOS-TOOLS] Scheduler PID: $SCHEDULER_PID"

shutdown()
{
    echo "[MOS-TOOLS] Shutting down..."

    if kill -0 "$SCHEDULER_PID" 2>/dev/null; then
        kill "$SCHEDULER_PID" 2>/dev/null || true
    fi
}

trap shutdown INT TERM EXIT

echo "[MOS-TOOLS] Starting WebUI..."

gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --threads 4 \
    --access-logfile - \
    --error-logfile - \
    app:app &

GUNICORN_PID=$!

echo "[MOS-TOOLS] Gunicorn PID: $GUNICORN_PID"

wait "$GUNICORN_PID"
RESULT=$?

shutdown

wait "$SCHEDULER_PID" 2>/dev/null || true

exit "$RESULT"
