from flask import Flask, jsonify, render_template, request

import json
import os
import socket
import re


app = Flask(__name__)


WEBUI_VERSION = "0.10"

SOCKET_PATH = "/run/mos-tools/agent.sock"
SCHEDULE_FILE = "/data/schedules.json"

MIN_SCHEDULE_INTERVAL = 10
MAX_SCHEDULE_INTERVAL = 31536000

DAILY_TIME_RE = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d$"
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
        "startup"
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

    if not isinstance(
        daily_time,
        str
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
        "missed": missed
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
            "startup"
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
            daily_time = schedule.get(
                "time"
            )

            if (
                not isinstance(
                    daily_time,
                    str
                )
                or
                not DAILY_TIME_RE.match(
                    daily_time
                )
            ):
                raise ValueError(
                    f"{job_id}: time must "
                    f"be HH:MM"
                )

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

            result[job_id] = {
                "enabled": enabled,
                "mode": "daily",
                "time": daily_time,
                "missed": missed
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
    if not isinstance(schedule, dict):
        return None

    if not schedule.get("enabled"):
        return None

    if schedule.get("mode") == "daily":
        daily_time = schedule.get("time", "03:00")
        return f"Daily · {daily_time}"

    if schedule.get("mode") == "startup":
        return "Startup"

    try:
        seconds = int(schedule.get("interval", 60))
    except (TypeError, ValueError):
        seconds = 60

    if seconds % 86400 == 0:
        value = seconds // 86400
        unit = "day" if value == 1 else "days"
    elif seconds % 3600 == 0:
        value = seconds // 3600
        unit = "hour" if value == 1 else "hours"
    elif seconds % 60 == 0:
        value = seconds // 60
        unit = "min"
    else:
        value = seconds
        unit = "sec"

    return f"Every {value} {unit}"


# ------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------

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
                "has_settings": False
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

        schedules = (
            get_tool_schedules(
                tool,
                metadata
            )
        )

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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )
