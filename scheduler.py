#!/usr/bin/env python3

import json
import os
import socket
import time

from datetime import datetime


VERSION = "0.7"

SOCKET_PATH = "/run/mos-tools/agent.sock"

SCHEDULE_FILE = "/data/schedules.json"
STATE_FILE = "/data/scheduler-state.json"

CHECK_INTERVAL = 1
MIN_INTERVAL = 10

last_interval_run = {}
startup_processed = set()


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

def timestamp():
    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def log(message):
    print(
        f"[SCHEDULER {timestamp()}] {message}",
        flush=True
    )


# ------------------------------------------------------------
# Agent
# ------------------------------------------------------------

def agent_request(payload, timeout=3):
    client = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    client.settimeout(timeout)

    try:
        client.connect(SOCKET_PATH)

        client.sendall(
            json.dumps(payload).encode("utf-8")
        )

        response = b""

        while True:
            chunk = client.recv(65536)

            if not chunk:
                break

            response += chunk

            if b"\n" in response:
                break

        if not response:
            raise RuntimeError(
                "Agent returned no response"
            )

        return json.loads(
            response.decode("utf-8")
        )

    finally:
        client.close()


# ------------------------------------------------------------
# JSON helpers
# ------------------------------------------------------------

def load_json(path, default):
    if not os.path.isfile(path):
        return default

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError(
                "JSON root must be an object"
            )

        return data

    except Exception as exc:
        log(
            f"Unable to read {path}: {exc}"
        )

        return default


def save_json(path, data):
    directory = os.path.dirname(path)

    os.makedirs(
        directory,
        exist_ok=True
    )

    temp = path + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            indent=2
        )

        f.write("\n")
        f.flush()

        os.fsync(
            f.fileno()
        )

    os.replace(
        temp,
        path
    )


# ------------------------------------------------------------
# Job execution
# ------------------------------------------------------------

def run_job(tool, job):
    try:
        status = agent_request({
            "action": "status",
            "tool": tool,
            "job": job
        })

        if not status.get("success"):
            log(
                f"{tool}/{job}: status failed: "
                f"{status.get('error')}"
            )

            return False

        if status.get("running"):
            log(
                f"{tool}/{job}: skipped, "
                f"already running"
            )

            return True

        result = agent_request({
            "action": "run",
            "tool": tool,
            "job": job
        })

        if result.get("success"):
            log(
                f"{tool}/{job}: started "
                f"pid={result.get('pid')}"
            )

            return True

        log(
            f"{tool}/{job}: start failed: "
            f"{result.get('error')}"
        )

        return False

    except Exception as exc:
        log(
            f"{tool}/{job}: agent error: {exc}"
        )

        return False


# ------------------------------------------------------------
# Interval schedules
# ------------------------------------------------------------

def check_interval(
    tool,
    job,
    schedule,
    monotonic_now
):
    try:
        interval = int(
            schedule.get(
                "interval",
                0
            )
        )

    except (TypeError, ValueError):
        log(
            f"{tool}/{job}: invalid interval"
        )

        return

    if interval < MIN_INTERVAL:
        log(
            f"{tool}/{job}: interval below "
            f"minimum of {MIN_INTERVAL} seconds"
        )

        return

    key = f"{tool}:{job}"

    previous = last_interval_run.get(
        key
    )

    if previous is None:
        last_interval_run[key] = (
            monotonic_now
        )

        log(
            f"{tool}/{job}: armed "
            f"interval ({interval}s)"
        )

        return

    if (
        monotonic_now - previous
        < interval
    ):
        return

    # Consume the slot before executing.
    # Failed jobs are not retried every second.
    last_interval_run[key] = (
        monotonic_now
    )

    run_job(
        tool,
        job
    )


# ------------------------------------------------------------
# Daily schedules
# ------------------------------------------------------------

def parse_daily_time(value):
    if not isinstance(value, str):
        raise ValueError(
            "time must be HH:MM"
        )

    parts = value.split(":")

    if len(parts) != 2:
        raise ValueError(
            "time must be HH:MM"
        )

    hour = int(parts[0])
    minute = int(parts[1])

    if not 0 <= hour <= 23:
        raise ValueError(
            "invalid hour"
        )

    if not 0 <= minute <= 59:
        raise ValueError(
            "invalid minute"
        )

    return hour, minute


def daily_slot_id(
    tool,
    job,
    now,
    hour,
    minute
):
    return (
        f"{tool}:{job}:"
        f"{now.date().isoformat()}:"
        f"{hour:02d}:{minute:02d}"
    )


def check_daily(
    tool,
    job,
    schedule,
    now,
    state
):
    try:
        hour, minute = parse_daily_time(
            schedule.get(
                "time",
                "00:00"
            )
        )

    except Exception as exc:
        log(
            f"{tool}/{job}: invalid daily "
            f"time: {exc}"
        )

        return False

    missed = schedule.get(
        "missed",
        "skip"
    )

    if missed not in (
        "run",
        "skip"
    ):
        missed = "skip"

    scheduled = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    slot = daily_slot_id(
        tool,
        job,
        now,
        hour,
        minute
    )

    key = f"{tool}:{job}"

    job_state = state.get(
        key,
        {}
    )

    if not isinstance(
        job_state,
        dict
    ):
        job_state = {}

    if (
        job_state.get("last_slot")
        == slot
    ):
        return False

    # The slot has not been reached yet.
    if now < scheduled:
        return False

    seconds_late = (
        now - scheduled
    ).total_seconds()

    # During the scheduled minute we always run.
    due_now = (
        0 <= seconds_late < 60
    )

    # Outside the scheduled minute this is a missed run.
    missed_run = (
        seconds_late >= 60
    )

    if missed_run and missed == "skip":
        state[key] = {
            "last_slot": slot,
            "processed_at": now.isoformat(
                timespec="seconds"
            ),
            "result": "missed-skipped"
        }

        log(
            f"{tool}/{job}: missed daily "
            f"slot {hour:02d}:{minute:02d}, "
            f"skipped"
        )

        return True

    if not due_now and not (
        missed_run and missed == "run"
    ):
        return False

    if missed_run:
        log(
            f"{tool}/{job}: running missed "
            f"daily slot "
            f"{hour:02d}:{minute:02d}"
        )

    success = run_job(
        tool,
        job
    )

    state[key] = {
        "last_slot": slot,
        "processed_at": now.isoformat(
            timespec="seconds"
        ),
        "result": (
            "started"
            if success
            else "failed"
        )
    }

    return True


# ------------------------------------------------------------
# Startup schedules
# ------------------------------------------------------------

def check_startup(tool, job):
    """
    Run a startup schedule once per scheduler process.
    Re-saving schedules does not re-run it. Restarting the scheduler does.
    """
    key = f"{tool}:{job}"

    if key in startup_processed:
        return False

    # Consume before execution so a failed job is not retried every second.
    startup_processed.add(key)

    log(
        f"{tool}/{job}: processing startup schedule"
    )

    run_job(tool, job)

    return False


# ------------------------------------------------------------
# Schedule dispatcher
# ------------------------------------------------------------

def check_job(
    tool,
    job,
    schedule,
    monotonic_now,
    wall_now,
    state
):
    if not isinstance(
        schedule,
        dict
    ):
        return False

    if not schedule.get(
        "enabled",
        False
    ):
        return False

    # V0.5 compatibility:
    # missing mode means interval.
    mode = schedule.get(
        "mode",
        "interval"
    )

    if mode == "interval":
        check_interval(
            tool,
            job,
            schedule,
            monotonic_now
        )

        return False

    if mode == "daily":
        return check_daily(
            tool,
            job,
            schedule,
            wall_now,
            state
        )

    if mode == "startup":
        return check_startup(
            tool,
            job
        )

    log(
        f"{tool}/{job}: unknown "
        f"schedule mode '{mode}'"
    )

    return False


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    log(
        f"MOS Tools Scheduler v{VERSION}"
    )

    log(
        f"Schedule file: {SCHEDULE_FILE}"
    )

    log(
        f"State file: {STATE_FILE}"
    )

    log(
        "Waiting for schedules..."
    )

    state = load_json(
        STATE_FILE,
        {}
    )

    while True:
        schedules = load_json(
            SCHEDULE_FILE,
            {}
        )

        monotonic_now = (
            time.monotonic()
        )

        wall_now = (
            datetime
            .now()
            .astimezone()
        )

        active_interval_keys = set()

        state_changed = False

        for tool, jobs in (
            schedules.items()
        ):
            if not isinstance(
                jobs,
                dict
            ):
                continue

            for job, schedule in (
                jobs.items()
            ):
                if not isinstance(
                    schedule,
                    dict
                ):
                    continue

                mode = schedule.get(
                    "mode",
                    "interval"
                )

                if (
                    schedule.get(
                        "enabled",
                        False
                    )
                    and
                    mode == "interval"
                ):
                    active_interval_keys.add(
                        f"{tool}:{job}"
                    )

                changed = check_job(
                    tool,
                    job,
                    schedule,
                    monotonic_now,
                    wall_now,
                    state
                )

                if changed:
                    state_changed = True

        # Forget removed/disabled/non-interval jobs.
        for key in list(
            last_interval_run
        ):
            if key not in active_interval_keys:
                last_interval_run.pop(
                    key,
                    None
                )

        if state_changed:
            try:
                save_json(
                    STATE_FILE,
                    state
                )

            except Exception as exc:
                log(
                    "Unable to save scheduler "
                    f"state: {exc}"
                )

        time.sleep(
            CHECK_INTERVAL
        )


if __name__ == "__main__":
    main()
