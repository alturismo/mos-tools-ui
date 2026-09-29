#!/usr/bin/env bash

AGENT="/mnt/cache/appdata/mos-tools/agent/agent.py"
SOCKET="/run/mos-tools/agent.sock"
LOG="/mnt/cache/appdata/mos-tools/logs/agent.log"

get_pids() {
    pgrep -f "^python3 ${AGENT}$" 2>/dev/null || true
}

start_agent() {
    local pids

    pids="$(get_pids)"

    if [ -n "$pids" ]; then
        echo "MOS Tools Agent already running: ${pids//$'\n'/ }"
        return 0
    fi

    mkdir -p "$(dirname "$LOG")"
    mkdir -p "$(dirname "$SOCKET")"

    rm -f "$SOCKET"

    nohup python3 "$AGENT" >>"$LOG" 2>&1 </dev/null &

    sleep 0.5

    pids="$(get_pids)"

    if [ -n "$pids" ]; then
        echo "MOS Tools Agent started: ${pids//$'\n'/ }"
        return 0
    fi

    echo "ERROR: MOS Tools Agent failed to start"
    return 1
}

stop_agent() {
    local pids pid

    pids="$(get_pids)"

    if [ -z "$pids" ]; then
        rm -f "$SOCKET"
        echo "MOS Tools Agent already stopped"
        return 0
    fi

    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done

    for _ in $(seq 1 30); do
        [ -z "$(get_pids)" ] && break
        sleep 0.1
    done

    pids="$(get_pids)"

    if [ -n "$pids" ]; then
        echo "Agent did not stop - forcing termination"

        for pid in $pids; do
            kill -9 "$pid" 2>/dev/null || true
        done
    fi

    rm -f "$SOCKET"

    echo "MOS Tools Agent stopped"
}

case "${1:-}" in
    start)
        start_agent
        ;;

    stop)
        stop_agent
        ;;

    restart)
        stop_agent
        sleep 0.2
        start_agent
        ;;

    status)
        PIDS="$(get_pids)"

        if [ -n "$PIDS" ]; then
            echo "MOS Tools Agent running: ${PIDS//$'\n'/ }"

            if [ -S "$SOCKET" ]; then
                echo "Socket: OK"
            else
                echo "Socket: MISSING"
            fi
        else
            echo "MOS Tools Agent stopped"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac