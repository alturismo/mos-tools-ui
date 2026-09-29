#!/bin/sh
set -e

MOS_ROOT="/mos-tools"
AGENT_SOURCE="/opt/mos-tools/agent"
AGENT_TARGET="${MOS_ROOT}/agent"

echo "[MOS-TOOLS] Initializing persistent directories..."

mkdir -p \
    "${MOS_ROOT}/agent" \
    "${MOS_ROOT}/data" \
    "${MOS_ROOT}/scripts" \
    "${MOS_ROOT}/logs" \
    "${MOS_ROOT}/status" \
    "${MOS_ROOT}/backups"

# ------------------------------------------------------------
# Persistent scheduler files
# ------------------------------------------------------------

if [ ! -f /data/schedules.json ]; then
    echo "[MOS-TOOLS] Creating schedules.json..."
    printf '{}\n' > /data/schedules.json
fi

if [ ! -f /data/scheduler-state.json ]; then
    echo "[MOS-TOOLS] Creating scheduler-state.json..."
    printf '{}\n' > /data/scheduler-state.json
fi

# ------------------------------------------------------------
# Host agent bootstrap
#
# Only install files that do not already exist.
# Never overwrite an existing host agent automatically.
# ------------------------------------------------------------

if [ -d "$AGENT_SOURCE" ]; then

    for source in "$AGENT_SOURCE"/*; do

        [ -e "$source" ] || continue

        name="$(basename "$source")"
        target="${AGENT_TARGET}/${name}"

        if [ ! -e "$target" ]; then
            echo "[MOS-TOOLS] Installing agent file: ${name}"
            cp -a "$source" "$target"
        fi

    done

fi

# Make host-side agent shell helpers executable.
find "$AGENT_TARGET" \
    -maxdepth 1 \
    -type f \
    -name "*.sh" \
    -exec chmod +x {} \; 2>/dev/null || true

# ------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------

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

    if [ -n "${GUNICORN_PID:-}" ] &&
       kill -0 "$GUNICORN_PID" 2>/dev/null; then
        kill "$GUNICORN_PID" 2>/dev/null || true
    fi
}

trap shutdown INT TERM EXIT

# ------------------------------------------------------------
# WebUI
# ------------------------------------------------------------

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
