#!/usr/bin/env python3

import json
import os
import socket
import time

from datetime import datetime, timedelta
import calendar
import re


VERSION = "0.8.1"

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
# Calendar schedules (weekly / monthly / cron)
# ------------------------------------------------------------

def calendar_slot_id(tool, job, scheduled):
    return f"{tool}:{job}:{scheduled.strftime('%Y-%m-%dT%H:%M')}"


def process_calendar_slot(tool, job, schedule, now, state, scheduled, label):
    if scheduled is None or now < scheduled:
        return False

    key = f"{tool}:{job}"
    slot = calendar_slot_id(tool, job, scheduled)
    job_state = state.get(key, {})
    if not isinstance(job_state, dict):
        job_state = {}
    if job_state.get("last_slot") == slot:
        return False

    seconds_late = (now - scheduled).total_seconds()
    missed = schedule.get("missed", "skip")
    if missed not in ("run", "skip"):
        missed = "skip"

    if seconds_late >= 60 and missed == "skip":
        state[key] = {
            "last_slot": slot,
            "processed_at": now.isoformat(timespec="seconds"),
            "result": "missed-skipped"
        }
        log(f"{tool}/{job}: missed {label} slot {scheduled.strftime('%Y-%m-%d %H:%M')}, skipped")
        return True

    success = run_job(tool, job)
    state[key] = {
        "last_slot": slot,
        "processed_at": now.isoformat(timespec="seconds"),
        "result": "started" if success else "failed"
    }
    return True


def check_weekly(tool, job, schedule, now, state):
    try:
        weekday = int(schedule.get("weekday", 0))
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday must be 0..6")
        hour, minute = parse_daily_time(schedule.get("time", "00:00"))
    except Exception as exc:
        log(f"{tool}/{job}: invalid weekly schedule: {exc}")
        return False

    days_back = (now.weekday() - weekday) % 7
    date = (now - timedelta(days=days_back)).date()
    scheduled = now.replace(year=date.year, month=date.month, day=date.day,
                            hour=hour, minute=minute, second=0, microsecond=0)
    return process_calendar_slot(tool, job, schedule, now, state, scheduled, "weekly")


def check_monthly(tool, job, schedule, now, state):
    try:
        hour, minute = parse_daily_time(schedule.get("time", "00:00"))
        value = schedule.get("day", 1)
        last_day = calendar.monthrange(now.year, now.month)[1]
        if str(value) == "last":
            day = last_day
        else:
            day = int(value)
            if day < 1 or day > 31:
                raise ValueError("day must be 1..31 or last")

            # A numeric day is literal. If that day does not exist in the
            # current month, this month has no slot. Use day="last" when
            # the intended behaviour is the actual last day of every month.
            if day > last_day:
                return False
    except Exception as exc:
        log(f"{tool}/{job}: invalid monthly schedule: {exc}")
        return False

    scheduled = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    return process_calendar_slot(tool, job, schedule, now, state, scheduled, "monthly")


def _cron_values(field, minimum, maximum):
    values = set()
    for part in field.split(','):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field")
        step = 1
        base = part
        if '/' in part:
            base, step_s = part.split('/', 1)
            step = int(step_s)
            if step < 1:
                raise ValueError("cron step must be >= 1")
        if base == '*':
            start, end = minimum, maximum
        elif '-' in base:
            a, b = base.split('-', 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron value out of range")
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expr, dt):
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron must contain 5 fields")
    minute, hour, dom, month, dow = fields
    minutes = _cron_values(minute, 0, 59)
    hours = _cron_values(hour, 0, 23)
    doms = _cron_values(dom, 1, 31)
    months = _cron_values(month, 1, 12)
    dows = _cron_values(dow, 0, 7)
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in dows or (cron_dow == 0 and 7 in dows)
    dom_match = dt.day in doms
    # Standard cron semantics: when both DOM and DOW are restricted, either may match.
    day_match = (dom_match and dow_match) if dom == '*' or dow == '*' else (dom_match or dow_match)
    return dt.minute in minutes and dt.hour in hours and dt.month in months and day_match


def check_cron(tool, job, schedule, now, state):
    expr = str(schedule.get("cron", "")).strip()
    try:
        # Validate once here and find the most recent matching minute, max 366 days back.
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("cron must contain 5 fields")
        candidate = now.replace(second=0, microsecond=0)
        scheduled = None
        for _ in range(366 * 24 * 60 + 1):
            if cron_matches(expr, candidate):
                scheduled = candidate
                break
            candidate -= timedelta(minutes=1)
        if scheduled is None:
            raise ValueError("no matching slot within 366 days")
    except Exception as exc:
        log(f"{tool}/{job}: invalid cron schedule '{expr}': {exc}")
        return False
    return process_calendar_slot(tool, job, schedule, now, state, scheduled, "cron")


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

    if mode == "weekly":
        return check_weekly(tool, job, schedule, wall_now, state)

    if mode == "monthly":
        return check_monthly(tool, job, schedule, wall_now, state)

    if mode == "cron":
        return check_cron(tool, job, schedule, wall_now, state)

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
