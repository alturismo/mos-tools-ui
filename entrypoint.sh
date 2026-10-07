#!/bin/sh
set -e

AGENT_SOURCE="/opt/mos-tools/agent"
AGENT_TARGET="/host-agent"
BACKUP_ROOT="/mos-tools-backups"

echo "[MOS-TOOLS] Initializing persistent directories..."

mkdir -p \
    /data \
    "$AGENT_TARGET" \
    "$BACKUP_ROOT"

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
# Host agent bootstrap / update
#
# Image agent:
#   /opt/mos-tools/agent
#
# Persistent host agent:
#   /host-agent
#
# Persistent backups:
#   /mos-tools-backups/agent/
# ------------------------------------------------------------

if [ -d "$AGENT_SOURCE" ]; then

    SOURCE_AGENT="${AGENT_SOURCE}/agent.py"
    TARGET_AGENT="${AGENT_TARGET}/agent.py"

    source_version=""
    target_version=""

    if [ -f "$SOURCE_AGENT" ]; then
        source_version="$(
            sed -n 's/^VERSION = ["'\'']\([^"'\'']*\)["'\'']/\1/p' \
                "$SOURCE_AGENT" | head -n 1
        )"
    fi

    if [ -f "$TARGET_AGENT" ]; then
        target_version="$(
            sed -n 's/^VERSION = ["'\'']\([^"'\'']*\)["'\'']/\1/p' \
                "$TARGET_AGENT" | head -n 1
        )"
    fi

    if [ ! -f "$TARGET_AGENT" ]; then

        echo "[MOS-TOOLS] Installing host agent: ${source_version:-unknown}"

        cp -a "$AGENT_SOURCE"/. "$AGENT_TARGET"/

    elif [ -n "$source_version" ] &&
         [ "$source_version" != "$target_version" ]; then

        echo "[MOS-TOOLS] Updating host agent: ${target_version:-unknown} -> ${source_version}"

        mkdir -p "${BACKUP_ROOT}/agent"

        BACKUP_DIR="${BACKUP_ROOT}/agent/agent-${target_version:-unknown}"

        if [ -e "$BACKUP_DIR" ]; then
            BACKUP_DIR="${BACKUP_DIR}-$(date +%Y%m%d-%H%M%S)"
        fi

        mkdir -p "$BACKUP_DIR"

        echo "[MOS-TOOLS] Backing up current host agent to: $BACKUP_DIR"

        cp -a "$AGENT_TARGET"/. "$BACKUP_DIR"/

        echo "[MOS-TOOLS] Installing image agent..."

        cp -a "$AGENT_SOURCE"/. "$AGENT_TARGET"/

        echo "[MOS-TOOLS] Host agent updated successfully."

    elif [ -z "$source_version" ]; then

        echo "[MOS-TOOLS] WARNING: Unable to determine image agent version."
        echo "[MOS-TOOLS] Existing host agent left unchanged."

    else

        echo "[MOS-TOOLS] Host agent already current: ${source_version}"

    fi

    # Install additional files introduced by the image.
    for source in "$AGENT_SOURCE"/*; do

        [ -e "$source" ] || continue

        name="$(basename "$source")"
        target="${AGENT_TARGET}/${name}"

        if [ ! -e "$target" ]; then
            echo "[MOS-TOOLS] Installing new agent file: ${name}"
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
