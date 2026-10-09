from flask import Flask, jsonify, render_template, request

import json
import os
import socket
import re


app = Flask(__name__)


WEBUI_VERSION = "0.15.1"

SOCKET_PATH = "/run/mos-tools/agent.sock"
SCHEDULE_FILE = "/data/schedules.json"

MIN_SCHEDULE_INTERVAL = 10
MAX_SCHEDULE_INTERVAL = 31536000

DAILY_TIME_RE = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d$"
)



CRON_FIELD_RE = re.compile(r"^[0-9*/,-]+$")


def validate_cron_field(value, minimum, maximum):
    if (
        not isinstance(value, str)
        or not value
        or not CRON_FIELD_RE.match(value)
    ):
        return False

    def number(token):
        if not token.isdigit():
            return None

        value = int(token)

        if minimum <= value <= maximum:
            return value

        return None

    for item in value.split(","):
        if not item:
            return False

        base, separator, step = item.partition("/")

        if separator:
            if (
                not step.isdigit()
                or int(step) < 1
                or "/" in step
            ):
                return False

        if base == "*":
            continue

        if "-" in base:
            parts = base.split("-")

            if len(parts) != 2:
                return False

            start = number(parts[0])
            end = number(parts[1])

            if (
                start is None
                or end is None
                or start > end
            ):
                return False

        elif number(base) is None:
            return False

    return True


def validate_cron_expression(value):
    if not isinstance(value, str):
        return False

    fields = value.split()

    if len(fields) != 5:
        return False

    limits = (
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 7)
    )

    return all(
        validate_cron_field(
            field,
            minimum,
            maximum
        )
        for field, (
            minimum,
            maximum
        ) in zip(fields, limits)
    )



# ------------------------------------------------------------
# Agent communication
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
# Display helpers
# ------------------------------------------------------------

def display_name(value):
    names = {
        "pwm-fan": "PWM Fan",
        "cpu": "CPU Fan",
        "hdd": "HDD Fan",
        "test": "Test",
        "run": "Run"
    }

    return names.get(
        value,
        value
        .replace("-", " ")
        .replace("_", " ")
        .title()
    )


# ------------------------------------------------------------
# Scheduler configuration
# ------------------------------------------------------------

def load_schedules():
    if not os.path.isfile(
        SCHEDULE_FILE
    ):
        return {}

    with open(
        SCHEDULE_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        data = json.load(f)

    if not isinstance(
        data,
        dict
    ):
        raise ValueError(
            "Invalid schedules.json"
        )

    return data


def save_schedules(data):
    directory = os.path.dirname(
        SCHEDULE_FILE
    )

    os.makedirs(
        directory,
        exist_ok=True
    )

    temp = SCHEDULE_FILE + ".tmp"

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
        SCHEDULE_FILE
    )


def get_tool_jobs(metadata):
    jobs = metadata.get(
        "jobs",
        {}
    )

    if not isinstance(
        jobs,
        dict
    ):
        return {}

    return jobs


def normalize_schedule(schedule):
    if not isinstance(
        schedule,
        dict
    ):
        schedule = {}

    mode = schedule.get(
        "mode",
        "interval"
    )

    if mode not in (
        "interval",
        "daily",
        "startup",
        "weekly",
        "monthly",
        "cron"
    ):
        mode = "interval"

    try:
        interval = int(
            schedule.get(
                "interval",
                60
            )
        )

    except (TypeError, ValueError):
        interval = 60

    daily_time = schedule.get(
        "time",
        "03:00"
    )

    if (
        not isinstance(
            daily_time,
            str
        )
        or not DAILY_TIME_RE.match(
            daily_time
        )
    ):
        daily_time = "03:00"

    missed = schedule.get(
        "missed",
        "skip"
    )

    if missed not in (
        "run",
        "skip"
    ):
        missed = "skip"

    try:
        weekday = int(
            schedule.get(
                "weekday",
                0
            )
        )

    except (TypeError, ValueError):
        weekday = 0

    if not 0 <= weekday <= 6:
        weekday = 0

    day = schedule.get(
        "day",
        1
    )

    if day != "last":
        try:
            day = int(day)

        except (TypeError, ValueError):
            day = 1

        if not 1 <= day <= 31:
            day = 1

    cron = schedule.get(
        "cron",
        "0 3 * * *"
    )

    if not validate_cron_expression(
        cron
    ):
        cron = "0 3 * * *"

    return {
        "enabled": bool(
            schedule.get(
                "enabled",
                False
            )
        ),
        "mode": mode,
        "interval": interval,
        "time": daily_time,
        "missed": missed,
        "weekday": weekday,
        "day": day,
        "cron": cron
    }

def get_tool_schedules(
    tool,
    metadata
):
    schedules = load_schedules()

    tool_data = schedules.get(
        tool,
        {}
    )

    if not isinstance(
        tool_data,
        dict
    ):
        tool_data = {}

    result = {}

    for job_id in get_tool_jobs(
        metadata
    ):
        result[job_id] = (
            normalize_schedule(
                tool_data.get(
                    job_id,
                    {}
                )
            )
        )

    return result


def validate_schedule_payload(
    metadata,
    values
):
    if not isinstance(
        values,
        dict
    ):
        raise ValueError(
            "Schedule data must be an object"
        )

    known_jobs = get_tool_jobs(
        metadata
    )

    result = {}

    def get_missed(
        job_id,
        schedule
    ):
        missed = schedule.get(
            "missed",
            "skip"
        )

        if missed not in (
            "run",
            "skip"
        ):
            raise ValueError(
                f"{job_id}: invalid "
                f"missed-run policy"
            )

        return missed

    def get_time(
        job_id,
        schedule
    ):
        value = schedule.get(
            "time"
        )

        if (
            not isinstance(
                value,
                str
            )
            or not DAILY_TIME_RE.match(
                value
            )
        ):
            raise ValueError(
                f"{job_id}: time must "
                f"be HH:MM"
            )

        return value

    for job_id in known_jobs:
        if job_id not in values:
            raise ValueError(
                f"Missing schedule for job: "
                f"{job_id}"
            )

        schedule = values[
            job_id
        ]

        if not isinstance(
            schedule,
            dict
        ):
            raise ValueError(
                f"{job_id}: invalid schedule"
            )

        enabled = schedule.get(
            "enabled",
            False
        )

        if not isinstance(
            enabled,
            bool
        ):
            raise ValueError(
                f"{job_id}: enabled must "
                f"be boolean"
            )

        mode = schedule.get(
            "mode",
            "interval"
        )

        if mode not in (
            "interval",
            "daily",
            "startup",
            "weekly",
            "monthly",
            "cron"
        ):
            raise ValueError(
                f"{job_id}: invalid "
                f"schedule mode"
            )

        if mode == "interval":
            try:
                interval = int(
                    schedule.get(
                        "interval"
                    )
                )

            except (
                TypeError,
                ValueError
            ):
                raise ValueError(
                    f"{job_id}: interval "
                    f"must be an integer"
                )

            if (
                interval
                < MIN_SCHEDULE_INTERVAL
            ):
                raise ValueError(
                    f"{job_id}: minimum "
                    f"interval is "
                    f"{MIN_SCHEDULE_INTERVAL} "
                    f"seconds"
                )

            if (
                interval
                > MAX_SCHEDULE_INTERVAL
            ):
                raise ValueError(
                    f"{job_id}: interval "
                    f"is too large"
                )

            result[job_id] = {
                "enabled": enabled,
                "mode": "interval",
                "interval": interval
            }

        elif mode == "daily":
            result[job_id] = {
                "enabled": enabled,
                "mode": "daily",
                "time": get_time(
                    job_id,
                    schedule
                ),
                "missed": get_missed(
                    job_id,
                    schedule
                )
            }

        elif mode == "weekly":
            try:
                weekday = int(
                    schedule.get(
                        "weekday"
                    )
                )

            except (
                TypeError,
                ValueError
            ):
                raise ValueError(
                    f"{job_id}: weekday "
                    f"must be 0..6"
                )

            if not 0 <= weekday <= 6:
                raise ValueError(
                    f"{job_id}: weekday "
                    f"must be 0..6"
                )

            result[job_id] = {
                "enabled": enabled,
                "mode": "weekly",
                "weekday": weekday,
                "time": get_time(
                    job_id,
                    schedule
                ),
                "missed": get_missed(
                    job_id,
                    schedule
                )
            }

        elif mode == "monthly":
            day = schedule.get(
                "day"
            )

            if day != "last":
                try:
                    day = int(day)

                except (
                    TypeError,
                    ValueError
                ):
                    raise ValueError(
                        f"{job_id}: day must "
                        f"be 1..31 or last"
                    )

                if not 1 <= day <= 31:
                    raise ValueError(
                        f"{job_id}: day must "
                        f"be 1..31 or last"
                    )

            result[job_id] = {
                "enabled": enabled,
                "mode": "monthly",
                "day": day,
                "time": get_time(
                    job_id,
                    schedule
                ),
                "missed": get_missed(
                    job_id,
                    schedule
                )
            }

        elif mode == "cron":
            cron = schedule.get(
                "cron"
            )

            if not validate_cron_expression(
                cron
            ):
                raise ValueError(
                    f"{job_id}: invalid cron "
                    f"expression (5 fields)"
                )

            result[job_id] = {
                "enabled": enabled,
                "mode": "cron",
                "cron": cron,
                "missed": get_missed(
                    job_id,
                    schedule
                )
            }

        else:
            result[job_id] = {
                "enabled": enabled,
                "mode": "startup"
            }

    for job_id in values:
        if job_id not in known_jobs:
            raise ValueError(
                f"Unknown job: {job_id}"
            )

    return result

def update_tool_schedules(
    tool,
    metadata,
    values
):
    validated = (
        validate_schedule_payload(
            metadata,
            values
        )
    )

    schedules = load_schedules()

    schedules[tool] = validated

    save_schedules(
        schedules
    )

    return validated


def config_value_enabled(value):
    """
    Treat common config values as booleans for metadata enabled_by.
    Missing/unknown values default to enabled so older tools remain visible.
    """
    if isinstance(value, bool):
        return value

    if value is None:
        return True

    value = str(value).strip().lower()

    if value in ("0", "false", "no", "off", "disabled"):
        return False

    if value in ("1", "true", "yes", "on", "enabled"):
        return True

    return True


def format_schedule_summary(schedule):
    if not isinstance(
        schedule,
        dict
    ):
        return None

    if not schedule.get(
        "enabled"
    ):
        return None

    mode = schedule.get(
        "mode"
    )

    if mode == "daily":
        daily_time = schedule.get(
            "time",
            "03:00"
        )

        return f"Daily · {daily_time}"

    if mode == "weekly":
        names = (
            "Mon",
            "Tue",
            "Wed",
            "Thu",
            "Fri",
            "Sat",
            "Sun"
        )

        try:
            weekday = int(
                schedule.get(
                    "weekday",
                    0
                )
            )

            name = (
                names[weekday]
                if 0 <= weekday <= 6
                else "?"
            )

        except (
            TypeError,
            ValueError
        ):
            name = "?"

        return (
            f"Weekly · {name} · "
            f"{schedule.get('time', '03:00')}"
        )

    if mode == "monthly":
        day = schedule.get(
            "day",
            1
        )

        label = (
            "Last day"
            if day == "last"
            else f"Day {day}"
        )

        return (
            f"Monthly · {label} · "
            f"{schedule.get('time', '03:00')}"
        )

    if mode == "cron":
        return (
            f"Advanced · "
            f"{schedule.get('cron', '')}"
        )

    if mode == "startup":
        return "Startup"

    try:
        seconds = int(
            schedule.get(
                "interval",
                60
            )
        )

    except (
        TypeError,
        ValueError
    ):
        seconds = 60

    if seconds % 86400 == 0:
        value = seconds // 86400

        unit = (
            "day"
            if value == 1
            else "days"
        )

    elif seconds % 3600 == 0:
        value = seconds // 3600

        unit = (
            "hour"
            if value == 1
            else "hours"
        )

    elif seconds % 60 == 0:
        value = seconds // 60
        unit = "min"

    else:
        value = seconds
        unit = "sec"

    return f"Every {value} {unit}"

def get_dashboard():
    dashboard = {
        "agent_online": False,
        "agent_version": None,
        "agent_error": None,
        "tools": []
    }

    try:
        ping = agent_request({
            "action": "ping"
        })

        if not ping.get("success"):
            raise RuntimeError(
                ping.get(
                    "error",
                    "PING failed"
                )
            )

        dashboard["agent_online"] = True
        dashboard["agent_version"] = ping.get("version")

        listing = agent_request({
            "action": "list"
        })

        if not listing.get("success"):
            raise RuntimeError(
                listing.get(
                    "error",
                    "LIST failed"
                )
            )

        all_schedules = load_schedules()

        for tool_name, jobs in (
            listing.get("tools", {}).items()
        ):
            tool_data = {
                "id": tool_name,
                "name": display_name(tool_name),
                "jobs": [],
                "has_settings": False,
                "builder_managed": False
            }

            metadata = {}
            config = {}

            try:
                meta_result = agent_request({
                    "action": "get_meta",
                    "tool": tool_name
                })

                if meta_result.get("success"):
                    metadata = meta_result.get(
                        "metadata",
                        {}
                    )

                    tool_data["builder_managed"] = metadata.get("builder") == {"version": 1}

                    tool_data["name"] = (
                        metadata.get("name")
                        or tool_data["name"]
                    )

                    tool_data["has_settings"] = bool(
                        metadata.get("config")
                        or metadata.get("jobs")
                    )

            except Exception:
                pass

            try:
                config_result = agent_request({
                    "action": "get_config",
                    "tool": tool_name
                })

                if config_result.get("success"):
                    config = config_result.get(
                        "config",
                        {}
                    )

            except Exception:
                pass

            metadata_jobs = get_tool_jobs(metadata)
            tool_schedules = all_schedules.get(
                tool_name,
                {}
            )

            if not isinstance(tool_schedules, dict):
                tool_schedules = {}

            for job_name in jobs:
                job_meta = metadata_jobs.get(
                    job_name,
                    {}
                )

                if not isinstance(job_meta, dict):
                    job_meta = {}

                enabled_by = job_meta.get(
                    "enabled_by"
                )

                if enabled_by:
                    if not config_value_enabled(
                        config.get(enabled_by)
                    ):
                        continue

                schedule = normalize_schedule(
                    tool_schedules.get(
                        job_name,
                        {}
                    )
                )

                try:
                    status = agent_request({
                        "action": "status",
                        "tool": tool_name,
                        "job": job_name
                    })

                    if status.get("success"):
                        job_data = {
                            **status,
                            "name": (
                                job_meta.get("name")
                                or display_name(job_name)
                            )
                        }

                    else:
                        job_data = {
                            "tool": tool_name,
                            "job": job_name,
                            "name": (
                                job_meta.get("name")
                                or display_name(job_name)
                            ),
                            "running": False,
                            "error": status.get(
                                "error",
                                "Status unavailable"
                            )
                        }

                except Exception as exc:
                    job_data = {
                        "tool": tool_name,
                        "job": job_name,
                        "name": (
                            job_meta.get("name")
                            or display_name(job_name)
                        ),
                        "running": False,
                        "error": str(exc)
                    }

                job_data["schedule"] = schedule
                job_data["schedule_summary"] = (
                    format_schedule_summary(schedule)
                )

                tool_data["jobs"].append(job_data)

            # Keep a tool visible when it has settings even if all optional
            # jobs are disabled. This preserves access to Settings.
            if tool_data["jobs"] or tool_data["has_settings"]:
                dashboard["tools"].append(tool_data)

    except Exception as exc:
        dashboard["agent_error"] = str(exc)

    return dashboard


# ------------------------------------------------------------
# Web routes
# ------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        dashboard=get_dashboard(),
        webui_version=WEBUI_VERSION
    )


@app.route(
    "/tools/<tool>/logs"
)
def tool_logs(tool):
    try:
        meta_result = agent_request({
            "action": "get_meta",
            "tool": tool
        })

        if not meta_result.get("success"):
            raise RuntimeError(
                meta_result.get(
                    "error",
                    "Unable to load metadata"
                )
            )

        return render_template(
            "logs.html",
            tool=tool,
            metadata=meta_result.get("metadata", {}),
            dashboard=get_dashboard(),
            webui_version=WEBUI_VERSION
        )

    except Exception as exc:
        return render_template(
            "logs.html",
            tool=tool,
            metadata={
                "name": display_name(tool),
                "jobs": {}
            },
            dashboard=get_dashboard(),
            error=str(exc),
            webui_version=WEBUI_VERSION
        ), 500


@app.route(
    "/tools/<tool>/settings"
)
def tool_settings(tool):
    try:
        meta_result = agent_request({
            "action": "get_meta",
            "tool": tool
        })

        if not meta_result.get(
            "success"
        ):
            raise RuntimeError(
                meta_result.get(
                    "error",
                    "Unable to load metadata"
                )
            )

        config_result = agent_request({
            "action": "get_config",
            "tool": tool
        })

        if not config_result.get(
            "success"
        ):
            raise RuntimeError(
                config_result.get(
                    "error",
                    "Unable to load configuration"
                )
            )

        metadata = meta_result.get(
            "metadata",
            {}
        )

        config = config_result.get(
            "config",
            {}
        )

        collections = {"jobs": []}
        if metadata.get("dynamic_jobs", {}).get("collection"):
            collection_result = agent_request({
                "action": "get_collections",
                "tool": tool
            })
            if collection_result.get("success"):
                collections = collection_result.get(
                    "collections",
                    collections
                )

        schedules = (
            get_tool_schedules(
                tool,
                metadata
            )
        )

        findings = {}
        if tool == "file-integrity":
            for job_id in metadata.get("jobs", {}):
                result = agent_request({
                    "action": "get_findings",
                    "tool": tool,
                    "job": job_id
                }, timeout=15)
                if result.get("success"):
                    findings[job_id] = result.get("data", {})

        dependencies = []

        if metadata.get("dependencies"):
            dependency_result = agent_request({
                "action": "get_dependencies",
                "tool": tool
            }, timeout=15)

            if dependency_result.get("success"):
                dependencies = dependency_result.get(
                    "dependencies",
                    []
                )

        tool_info = {
            "readme": None,
            "panels": []
        }

        if metadata.get("info"):
            info_result = agent_request({
                "action": "get_info",
                "tool": tool
            }, timeout=15)

            if info_result.get("success"):
                tool_info = info_result.get(
                    "info",
                    tool_info
                )

        return render_template(
            "settings.html",
            tool=tool,
            metadata=metadata,
            config=config,
            collections=collections,
            findings=findings,
            schedules=schedules,
            dependencies=dependencies,
            tool_info=tool_info,
            dashboard=get_dashboard(),
            webui_version=WEBUI_VERSION
        )

    except Exception as exc:
        return render_template(
            "settings.html",
            tool=tool,
            metadata={
                "name":
                    display_name(tool),
                "config": {},
                "jobs": {}
            },
            config={},
            collections={"jobs": []},
            findings={},
            schedules={},
            dependencies=[],
            tool_info={
                "readme": None,
                "panels": []
            },
            error=str(exc),
            dashboard=get_dashboard(),
            webui_version=WEBUI_VERSION
        ), 500


# ------------------------------------------------------------
# API - Health
# ------------------------------------------------------------

@app.route("/api/health")
def health():
    try:
        ping = agent_request({
            "action": "ping"
        })

        return jsonify({
            "success": True,
            "webui": WEBUI_VERSION,
            "agent": ping
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "webui": WEBUI_VERSION,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Agent restart
# ------------------------------------------------------------

@app.route("/api/agent/restart", methods=["POST"])
def restart_agent():
    try:
        result = agent_request({"action": "restart"})
        return jsonify(result), (
            200 if result.get("success") else 500
        )
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Jobs
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/<job>/run",
    methods=["POST"]
)
def run_job(tool, job):
    try:
        result = agent_request({
            "action": "run",
            "tool": tool,
            "job": job
        })

        code = (
            200
            if result.get("success")
            else 409
        )

        return jsonify(
            result
        ), code

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


@app.route(
    "/api/tools/<tool>/<job>/stop",
    methods=["POST"]
)
def stop_job(tool, job):
    try:
        result = agent_request({
            "action": "stop",
            "tool": tool,
            "job": job
        })

        code = (
            200
            if result.get("success")
            else 409
        )

        return jsonify(
            result
        ), code

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


@app.route(
    "/api/tools/<tool>/<job>/status"
)
def job_status(tool, job):
    try:
        result = agent_request({
            "action": "status",
            "tool": tool,
            "job": job
        })

        return jsonify(
            result
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Job logs
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/<job>/log"
)
def job_log(tool, job):
    try:
        lines = request.args.get(
            "lines",
            200,
            type=int
        )

        result = agent_request({
            "action": "get_log",
            "tool": tool,
            "job": job,
            "lines": lines
        })

        code = (
            200
            if result.get("success")
            else 400
        )

        return jsonify(
            result
        ), code

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503



# ------------------------------------------------------------
# API - File Integrity findings
# ------------------------------------------------------------

@app.route("/api/tools/<tool>/findings/<job>")
def tool_findings(tool, job):
    try:
        result = agent_request({
            "action": "get_findings",
            "tool": tool,
            "job": job
        }, timeout=15)
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 503


@app.route("/api/tools/<tool>/findings/<job>/accept", methods=["POST"])
def accept_tool_finding(tool, job):
    try:
        payload = request.get_json()
        if not isinstance(payload, dict) or not payload.get("path"):
            return jsonify({"success": False, "error": "Missing path"}), 400
        result = agent_request({
            "action": "accept_finding",
            "tool": tool,
            "job": job,
            "path": payload["path"]
        }, timeout=3600)
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 503


# ------------------------------------------------------------
# API - Dynamic collections
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/collections",
    methods=["POST"]
)
def save_tool_collections(tool):
    try:
        values = request.get_json()
        if not isinstance(values, dict):
            return jsonify({
                "success": False,
                "error": "Invalid collections data"
            }), 400

        result = agent_request({
            "action": "set_collections",
            "tool": tool,
            "collections": values
        })

        return jsonify(result), (
            200 if result.get("success") else 400
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Tool configuration
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/config",
    methods=["POST"]
)
def save_tool_config(tool):
    try:
        values = request.get_json()

        if not isinstance(
            values,
            dict
        ):
            return jsonify({
                "success": False,
                "error":
                    "Invalid configuration data"
            }), 400

        result = agent_request({
            "action": "set_config",
            "tool": tool,
            "values": values
        })

        code = (
            200
            if result.get("success")
            else 400
        )

        return jsonify(
            result
        ), code

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Tool dependencies
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/dependencies/install",
    methods=["POST"]
)
def install_tool_dependencies(tool):
    try:
        result = agent_request({
            "action": "install_dependencies",
            "tool": tool
        }, timeout=360)

        return jsonify(result), (
            200 if result.get("success") else 400
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# ------------------------------------------------------------
# API - Scheduler
# ------------------------------------------------------------

@app.route(
    "/api/tools/<tool>/schedule",
    methods=["POST"]
)
def save_tool_schedule(tool):
    try:
        meta_result = agent_request({
            "action": "get_meta",
            "tool": tool
        })

        if not meta_result.get(
            "success"
        ):
            raise RuntimeError(
                meta_result.get(
                    "error",
                    "Unable to load metadata"
                )
            )

        metadata = meta_result.get(
            "metadata",
            {}
        )

        values = request.get_json()

        schedules = (
            update_tool_schedules(
                tool,
                metadata,
                values
            )
        )

        return jsonify({
            "success": True,
            "tool": tool,
            "schedules": schedules
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 503


# MOS Tool Builder (builder-managed tools only)
@app.route("/builder")
@app.route("/builder/<tool>")
def builder_page(tool=None):
    if tool is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool):
        return "Invalid tool id", 400
    return render_template("builder.html", tool=tool, webui_version=WEBUI_VERSION)


@app.route("/api/builder/<tool>", methods=["GET"])
def builder_load(tool):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool):
        return jsonify(success=False, error="Invalid tool id"), 400
    try:
        result = agent_request({"action": "builder_get", "tool": tool}, timeout=10)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503


@app.route("/api/builder", methods=["POST"])
@app.route("/api/builder/<tool>", methods=["PUT"])
def builder_save(tool=None):
    if request.content_length is not None and request.content_length > 800000:
        return jsonify(success=False, error="Payload too large"), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error="Invalid JSON payload"), 400
    if tool is not None and data.get("id") != tool:
        return jsonify(success=False, error="Tool ID cannot be changed"), 400
    if len(request.get_data()) > 800000:
        return jsonify(success=False, error="Payload too large"), 413
    try:
        action = "builder_update" if tool is not None else "builder_create"
        result = agent_request({"action": action, "data": data}, timeout=30)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503



@app.route("/api/builder/backup-tools", methods=["GET"])
def builder_backup_tools_api():
    try:
        result = agent_request({"action": "builder_backup_tools"}, timeout=30)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503


@app.route("/api/builder/<tool>/backups", methods=["GET"])
def builder_backup_list(tool):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tool):
        return jsonify(success=False, error="Invalid tool id"), 400
    try:
        result = agent_request({"action": "builder_backups", "tool": tool}, timeout=20)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503


@app.route("/api/builder/<tool>/restore", methods=["POST"])
def builder_restore_api(tool):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tool):
        return jsonify(success=False, error="Invalid tool id"), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("backup"), str):
        return jsonify(success=False, error="Invalid backup selection"), 400
    try:
        result = agent_request({"action": "builder_restore", "tool": tool,
                                "backup": data["backup"]}, timeout=30)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503


@app.route("/api/builder/<tool>", methods=["DELETE"])
def builder_delete_api(tool):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tool):
        return jsonify(success=False, error="Invalid tool id"), 400
    try:
        result = agent_request({"action": "builder_delete", "tool": tool}, timeout=30)
        return jsonify(result), 200 if result.get("success") else 400
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )
