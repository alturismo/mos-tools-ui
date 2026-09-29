#!/usr/bin/env python3

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from datetime import datetime
from pathlib import Path
import importlib.util
import signal


VERSION = "1.10.1"

SOCKET_PATH = "/run/mos-tools/agent.sock"
RESTART_HELPER = Path("/mnt/cache/appdata/mos-tools/agent/restart-agent.sh").resolve()

BASE_ROOT = Path("/mnt/cache/appdata/mos-tools").resolve()
SCRIPT_ROOT = (BASE_ROOT / "scripts").resolve()
LOG_ROOT = BASE_ROOT / "logs"
STATUS_ROOT = BASE_ROOT / "status"

OPTIONAL_PACKAGE_ROOT = Path("/boot/optional/packages").resolve()
PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
dependency_install_lock = threading.Lock()

running = {}
running_lock = threading.Lock()


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def console(message):
    print(f"[{timestamp()}] {message}", flush=True)


def send_response(connection, data):
    try:
        connection.sendall(
            (json.dumps(data) + "\n").encode("utf-8")
        )
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
        console(f"RESPONSE client disconnected: {type(exc).__name__}: {exc}")


def job_key(tool, job):
    return f"{tool}:{job}"


def safe_name(value):
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or value in (".", "..")
    ):
        raise ValueError("Invalid name")

    # Beschränkt Tool-/Jobnamen bewusst auf harmlose Zeichen.
    if not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError("Invalid name")

    return value


def status_file(tool, job):
    return STATUS_ROOT / tool / f"{job}.json"


def log_file(tool, job):
    return LOG_ROOT / tool / f"{job}.log"


def write_status(tool, job, data):
    directory = STATUS_ROOT / tool
    directory.mkdir(parents=True, exist_ok=True)

    path = status_file(tool, job)
    temp = path.with_suffix(".json.tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    os.replace(temp, path)


def empty_status(tool, job):
    return {
        "tool": tool,
        "job": job,
        "running": False,
        "last_run": None,
        "finished": None,
        "exit_code": None,
        "duration": None,
        "pid": None
    }


def read_status(tool, job):
    path = status_file(tool, job)

    if not path.is_file():
        return empty_status(tool, job)

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        result = empty_status(tool, job)
        result["status_error"] = True
        return result


# ------------------------------------------------------------
# Tool / Job discovery
# ------------------------------------------------------------

def get_script(tool, job):
    tool = safe_name(tool)
    job = safe_name(job)

    tool_dir = (SCRIPT_ROOT / tool).resolve()

    try:
        tool_dir.relative_to(SCRIPT_ROOT)
    except ValueError:
        raise ValueError("Tool outside allowed root")

    if not tool_dir.is_dir():
        raise FileNotFoundError("Tool not found")

    script = (tool_dir / f"{job}.sh").resolve()

    try:
        script.relative_to(tool_dir)
    except ValueError:
        raise ValueError("Script outside allowed tool directory")

    if not script.is_file():
        raise FileNotFoundError(
            f"Job '{job}' not found for tool '{tool}'"
        )

    if not os.access(script, os.X_OK):
        raise PermissionError(
            f"{tool}/{job}.sh is not executable"
        )

    return script


def get_tools():
    result = {}

    if not SCRIPT_ROOT.is_dir():
        return result

    for tool_dir in sorted(SCRIPT_ROOT.iterdir()):

        if not tool_dir.is_dir():
            continue

        # Symlink-Verzeichnisse ignorieren.
        if tool_dir.is_symlink():
            continue

        jobs = []

        for script in sorted(tool_dir.glob("*.sh")):

            if not script.is_file() or script.is_symlink():
                continue

            try:
                resolved = script.resolve()
                resolved.relative_to(tool_dir.resolve())
            except ValueError:
                continue

            jobs.append(script.stem)

        if jobs:
            result[tool_dir.name] = jobs

    return result


# ------------------------------------------------------------
# Process handling
# ------------------------------------------------------------

def process_watcher(
    tool,
    job,
    process,
    started_ts,
    started_string,
    logfile_handle
):
    exit_code = process.wait()

    finished_ts = time.time()
    finished_string = timestamp()
    duration = round(finished_ts - started_ts, 3)

    logfile_handle.write(
        f"\n[{finished_string}] "
        f"FINISHED exit={exit_code} duration={duration}s\n"
    )
    logfile_handle.flush()
    logfile_handle.close()

    status = {
        "tool": tool,
        "job": job,
        "running": False,
        "last_run": started_string,
        "finished": finished_string,
        "exit_code": exit_code,
        "duration": duration,
        "pid": process.pid
    }

    write_status(tool, job, status)

    key = job_key(tool, job)

    with running_lock:
        running.pop(key, None)

    console(
        f"FINISHED {tool}/{job} "
        f"pid={process.pid} "
        f"exit={exit_code} "
        f"duration={duration}s"
    )



def stop_job(tool, job):
    key = job_key(tool, job)

    with running_lock:
        existing = running.get(key)

        if not existing:
            raise RuntimeError(
                f"Job '{tool}/{job}' is not running"
            )

        process = existing["process"]

        if process.poll() is not None:
            running.pop(key, None)
            raise RuntimeError(
                f"Job '{tool}/{job}' is not running"
            )

        pid = process.pid

    # Jobs are started with start_new_session=True, so terminate the complete
    # process group. This also stops child processes spawned by shell scripts.
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        console(
            f"STOP {tool}/{job} pid={pid} -> SIGKILL after timeout"
        )
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    console(f"STOP {tool}/{job} -> pid={pid}")

    return pid

def run_job(tool, job):
    script = get_script(tool, job)
    key = job_key(tool, job)

    with running_lock:

        existing = running.get(key)

        if existing:
            process = existing["process"]

            if process.poll() is None:
                raise RuntimeError(
                    f"Job '{tool}/{job}' is already running "
                    f"(pid {process.pid})"
                )

            running.pop(key, None)

        started_string = timestamp()
        started_ts = time.time()

        directory = LOG_ROOT / tool
        directory.mkdir(parents=True, exist_ok=True)

        logfile_handle = open(
            log_file(tool, job),
            "a",
            encoding="utf-8",
            buffering=1
        )

        logfile_handle.write(
            "\n"
            "============================================================\n"
            f"[{started_string}] START {tool}/{job}\n"
            "============================================================\n"
        )
        logfile_handle.flush()

        process = subprocess.Popen(
            [str(script)],
            cwd=str(script.parent),
            stdout=logfile_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

        running[key] = {
            "process": process,
            "pid": process.pid,
            "started": started_string,
            "started_ts": started_ts
        }

        write_status(
            tool,
            job,
            {
                "tool": tool,
                "job": job,
                "running": True,
                "last_run": started_string,
                "finished": None,
                "exit_code": None,
                "duration": None,
                "pid": process.pid
            }
        )

    watcher = threading.Thread(
        target=process_watcher,
        args=(
            tool,
            job,
            process,
            started_ts,
            started_string,
            logfile_handle
        ),
        daemon=True
    )

    watcher.start()

    console(f"RUN {tool}/{job} -> pid={process.pid}")

    return process.pid


def get_job_status(tool, job):
    # Validiert gleichzeitig Tool und Job.
    get_script(tool, job)

    key = job_key(tool, job)

    with running_lock:

        entry = running.get(key)

        if entry:
            process = entry["process"]

            if process.poll() is None:
                return {
                    "tool": tool,
                    "job": job,
                    "running": True,
                    "last_run": entry["started"],
                    "finished": None,
                    "exit_code": None,
                    "duration": round(
                        time.time() - entry["started_ts"],
                        3
                    ),
                    "pid": process.pid
                }

    return read_status(tool, job)

# ------------------------------------------------------------
# Plugin metadata / configuration
# ------------------------------------------------------------

def get_tool_dir(tool):
    tool = safe_name(tool)

    tool_dir = (SCRIPT_ROOT / tool).resolve()

    try:
        tool_dir.relative_to(SCRIPT_ROOT)
    except ValueError:
        raise ValueError("Tool outside allowed root")

    if not tool_dir.is_dir():
        raise FileNotFoundError("Tool not found")

    return tool_dir


def get_metadata(tool):
    tool_dir = get_tool_dir(tool)

    path = tool_dir / "tool.json"

    if not path.is_file():
        return {
            "id": tool,
            "name": tool,
            "config": {}
        }

    with open(path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    if not isinstance(metadata, dict):
        raise ValueError("Invalid tool.json")

    return metadata


def get_config_schema(tool, values=None):
    """
    Build the complete configuration schema.

    Repeated sections are expanded from their repeat counter.  When
    saving, submitted values take precedence over the value currently
    stored in config.conf so reading and writing use exactly the same
    schema logic.
    """
    metadata = get_metadata(tool)

    schema = {}

    # Static fields first. Repeat counters must be known before repeated
    # sections can be expanded.
    for section in metadata.get("config", {}).values():

        if section.get("repeat"):
            continue

        for field in section.get("fields", []):

            key = field.get("key")

            if not key:
                continue

            schema[key] = field

    raw_values = {}

    # Existing config is the normal source for repeat counters.
    path = get_tool_dir(tool) / "config.conf"

    if path.is_file():

        with open(path, "r", encoding="utf-8") as f:

            for raw_line in f:

                line = raw_line.strip()

                if (
                    not line
                    or line.startswith("#")
                    or "=" not in line
                ):
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip()

                if (
                    len(value) >= 2
                    and value[0] == '"'
                    and value[-1] == '"'
                ):
                    value = value[1:-1]

                raw_values[key] = value

    # Submitted values override the existing config while saving.
    if isinstance(values, dict):

        for key, value in values.items():
            raw_values[key] = value

    for section in metadata.get("config", {}).values():

        repeat_key = section.get("repeat")

        if not repeat_key:
            continue

        counter_field = schema.get(repeat_key)

        if not counter_field:
            raise ValueError(
                f"Repeat counter '{repeat_key}' is not defined"
            )

        raw_count = raw_values.get(
            repeat_key,
            counter_field.get("default", 0)
        )

        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            raise ValueError(
                f"{repeat_key}: value must be an integer"
            )

        minimum = counter_field.get("min")
        maximum = counter_field.get("max")

        if minimum is not None and count < minimum:
            raise ValueError(
                f"{repeat_key}: minimum value is {minimum}"
            )

        if maximum is not None and count > maximum:
            raise ValueError(
                f"{repeat_key}: maximum value is {maximum}"
            )

        prefix = section.get("prefix", "item")

        for index in range(1, count + 1):

            for template in section.get("fields", []):

                base_key = template.get("key")

                if not base_key:
                    continue

                field = dict(template)
                key = f"{prefix}_{index}_{base_key}"

                field["key"] = key
                field["repeat_index"] = index
                field["repeat_base_key"] = base_key

                schema[key] = field

    return schema

def parse_config(tool):
    tool_dir = get_tool_dir(tool)
    path = tool_dir / "config.conf"

    schema = get_config_schema(tool)

    result = {}

    if path.is_file():

        with open(path, "r", encoding="utf-8") as f:

            for raw_line in f:

                line = raw_line.strip()

                if (
                    not line
                    or line.startswith("#")
                    or "=" not in line
                ):
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip()

                # Only expose variables defined by tool.json
                if key not in schema:
                    continue

                if (
                    len(value) >= 2
                    and value[0] == '"'
                    and value[-1] == '"'
                ):
                    value = value[1:-1]

                field = schema[key]

                if field.get("type") == "integer":

                    try:
                        value = int(value)
                    except ValueError:
                        raise ValueError(
                            f"Invalid integer in config: {key}"
                        )

                if field.get("type") == "boolean":
                    normalized = str(value).strip().lower()
                    if normalized in ("true", "1", "yes", "on"):
                        value = True
                    elif normalized in ("false", "0", "no", "off"):
                        value = False
                    else:
                        raise ValueError(f"Invalid boolean in config: {key}")

                result[key] = value

    # Fill missing values from defaults
    for key, field in schema.items():

        if key not in result and "default" in field:
            result[key] = field["default"]

    return result


def validate_config_value(key, value, field):

    field_type = field.get("type", "text")

    if field_type == "integer":

        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{key}: value must be an integer"
            )

        minimum = field.get("min")
        maximum = field.get("max")

        if minimum is not None and value < minimum:
            raise ValueError(
                f"{key}: minimum value is {minimum}"
            )

        if maximum is not None and value > maximum:
            raise ValueError(
                f"{key}: maximum value is {maximum}"
            )

        return value


    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{key}: value must be boolean")


    if field_type == "choice":

        if not isinstance(value, str):
            value = str(value)

        choices = field.get("choices", [])

        allowed_values = [
            str(choice.get("value"))
            for choice in choices
            if isinstance(choice, dict)
            and "value" in choice
        ]

        if value not in allowed_values:
            raise ValueError(
                f"{key}: invalid choice '{value}'"
            )

        return value


    if field_type == "text":

        if not isinstance(value, str):
            value = str(value)

        if len(value) > 256:
            raise ValueError(
                f"{key}: value is too long"
            )

        # config.conf is sourced by root shell scripts.
        # Do not allow shell metacharacters.
        forbidden = [
            "\n",
            "\r",
            "`",
            "$",
            "\\",
            '"',
            "'",
            ";",
            "|",
            "&",
            "<",
            ">",
            "(",
            ")",
            "{",
            "}"
        ]

        for char in forbidden:

            if char in value:
                raise ValueError(
                    f"{key}: invalid character"
                )

        return value


    if field_type == "arguments":

        if not isinstance(value, str):
            value = str(value)

        if len(value) > 2048:
            raise ValueError(
                f"{key}: value is too long"
            )

        # config.conf is sourced by root shell scripts.
        # Allow FFmpeg argument syntax, but reject characters that
        # can escape or expand a double-quoted shell assignment.
        forbidden = [
            "\n",
            "\r",
            "`",
            "$",
            "\\",
            '"'
        ]

        for char in forbidden:

            if char in value:
                raise ValueError(
                    f"{key}: invalid character"
                )

        return value


    raise ValueError(
        f"{key}: unsupported field type '{field_type}'"
    )


def shell_quote(value):
    """
    Values reaching this function have already been validated.
    We nevertheless always write quoted values.
    """
    if isinstance(value, bool):
        value = "true" if value else "false"

    return '"' + str(value) + '"'


def save_config(tool, values):

    if not isinstance(values, dict):
        raise ValueError("values must be an object")

    tool_dir = get_tool_dir(tool)

    schema = get_config_schema(tool, values)

    if not schema:
        raise ValueError(
            f"Tool '{tool}' has no configurable fields"
        )

    # Start from existing/default config so partial updates work.
    config = parse_config(tool)

    for key, value in values.items():

        if key not in schema:
            raise ValueError(
                f"Unknown configuration option: {key}"
            )

        config[key] = validate_config_value(
            key,
            value,
            schema[key]
        )

    # Validate the complete result once more.
    for key, field in schema.items():

        if key not in config:
            raise ValueError(
                f"Missing configuration option: {key}"
            )

        config[key] = validate_config_value(
            key,
            config[key],
            field
        )

    path = tool_dir / "config.conf"
    temp = tool_dir / "config.conf.tmp"

    with open(temp, "w", encoding="utf-8") as f:

        f.write(
            "# ------------------------------------------------------------\n"
            "# MOS Tools configuration\n"
            "# Managed by MOS Tools Agent - DO NOT EDIT WHILE SAVING\n"
            "# ------------------------------------------------------------\n\n"
        )

        # Preserve schema ordering from tool.json.
        metadata = get_metadata(tool)

        for section in metadata.get(
            "config", {}
        ).values():

            title = section.get("title")
            repeat_key = section.get("repeat")

            if not repeat_key:

                if title:
                    f.write(f"# {title}\n")

                for field in section.get("fields", []):
                    key = field["key"]
                    f.write(
                        f"{key}={shell_quote(config[key])}\n"
                    )

                f.write("\n")
                continue

            count = int(config[repeat_key])
            prefix = section.get("prefix", "item")

            for index in range(1, count + 1):

                if title:
                    f.write(f"# {title} {index}\n")

                for field in section.get("fields", []):
                    key = (
                        f"{prefix}_{index}_{field['key']}"
                    )
                    f.write(
                        f"{key}={shell_quote(config[key])}\n"
                    )

                f.write("\n")

        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, path)

    console(f"CONFIG SAVE {tool}")

    return config

# ------------------------------------------------------------
# API
# ------------------------------------------------------------


# ------------------------------------------------------------
# Tool information / README
# ------------------------------------------------------------

def get_tool_info(tool):
    """
    Read optional help text and execute only info scripts explicitly
    declared by this tool's tool.json. Paths are confined to the tool
    directory; arbitrary commands from metadata are not supported.
    """
    tool_dir = get_tool_dir(tool)
    metadata = get_metadata(tool)
    info_meta = metadata.get("info", {})

    if not isinstance(info_meta, dict):
        return {
            "readme": None,
            "panels": []
        }

    result = {
        "readme": None,
        "panels": []
    }

    readme_name = info_meta.get("readme")

    if readme_name:
        readme_path = (tool_dir / readme_name).resolve()

        try:
            readme_path.relative_to(tool_dir)
        except ValueError:
            raise ValueError("Invalid README path")

        if readme_path.is_file():
            result["readme"] = readme_path.read_text(
                encoding="utf-8"
            )

    panels = info_meta.get("panels", [])

    if not isinstance(panels, list):
        raise ValueError("info.panels must be a list")

    for panel in panels:
        if not isinstance(panel, dict):
            continue

        script_name = panel.get("script")
        title = panel.get("title")

        if not script_name or not title:
            continue

        script_path = (tool_dir / script_name).resolve()

        try:
            script_path.relative_to(tool_dir)
        except ValueError:
            raise ValueError("Invalid info script path")

        panel_result = {
            "id": panel.get("id"),
            "title": title,
            "description": panel.get("description"),
            "output": "",
            "error": None
        }

        if not script_path.is_file():
            panel_result["error"] = (
                f"Info script not found: {script_name}"
            )
        else:
            try:
                completed = subprocess.run(
                    ["/bin/bash", str(script_path)],
                    cwd=str(tool_dir),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    check=False
                )

                panel_result["output"] = (
                    completed.stdout.rstrip()
                )

                if completed.returncode != 0:
                    panel_result["error"] = (
                        f"Exited with code "
                        f"{completed.returncode}"
                    )

            except subprocess.TimeoutExpired:
                panel_result["error"] = (
                    "Info script timed out"
                )

        result["panels"].append(panel_result)

    return result



# ------------------------------------------------------------
# Tool dependencies
# ------------------------------------------------------------

def get_dependencies(tool):
    metadata = get_metadata(tool)
    raw = metadata.get("dependencies", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("dependencies must be a list")

    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid dependency definition")
        command = item.get("command")
        package = item.get("package")
        if (not isinstance(command, str) or not command or "/" in command
                or not isinstance(package, str)
                or (package != "__system__" and not PACKAGE_NAME_RE.fullmatch(package))):
            raise ValueError("Invalid dependency definition")
        condition = item.get("when")

        if condition is not None:
            if not isinstance(condition, dict):
                raise ValueError("dependency when must be an object")

            field = condition.get("field")
            if not isinstance(field, str) or not field:
                raise ValueError("dependency when.field must be a string")

            if "equals" not in condition:
                raise ValueError("dependency when.equals is required")

        result.append({
            "command": command,
            "package": package,
            "when": condition
        })
    return result


def persistent_packages():
    """Return persistent package names without invoking dpkg-deb.

    Debian package files use <package>_<version>_<arch>.deb.  For the UI
    status path we only need the package name, so parsing the filename keeps
    DEPENDENCIES GET local and non-blocking.
    """
    result = set()

    if not OPTIONAL_PACKAGE_ROOT.is_dir():
        return result

    for path in OPTIONAL_PACKAGE_ROOT.glob("*.deb"):
        name = path.name.split("_", 1)[0].strip()

        if PACKAGE_NAME_RE.fullmatch(name):
            result.add(name)

    return result



def dependency_is_installed(command):
    """Check a dependency locally, without APT/repository access."""
    if command.startswith("python:"):
        module = command.split(":", 1)[1].strip()
        if not module:
            return False
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            return False

    return shutil.which(command) is not None

def dependency_is_required(tool, dependency):
    condition = dependency.get("when")

    if condition is None:
        return True

    config = parse_config(tool)
    current = config.get(condition["field"])
    expected = condition["equals"]

    if isinstance(expected, bool):
        return str(current).strip().lower() in (
            ("true", "1", "yes", "on")
            if expected
            else ("false", "0", "no", "off", "")
        )

    return str(current) == str(expected)


def get_dependency_status(tool):
    persistent = persistent_packages()
    result = []

    for dep in get_dependencies(tool):
        required = dependency_is_required(tool, dep)
        installed = dependency_is_installed(dep["command"])

        result.append({
            "command": dep["command"],
            "package": dep["package"],
            "required": required,
            "installed": installed,
            "persistent": dep["package"] in persistent
        })

    return result



def apt_dependency_closure(packages):
    """Return packages APT says are needed in addition to the requested set."""
    proc = subprocess.run(
        ["apt-get", "-s", "-o", "Debug::NoLocking=1", "install", *packages],
        input="y\n",
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "APT dependency resolution failed: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[-1500:]
        )

    resolved = set(packages)
    for line in proc.stdout.splitlines():
        match = re.match(r"^Inst\s+(\S+)", line)
        if match:
            name = match.group(1).split(":", 1)[0]
            if PACKAGE_NAME_RE.fullmatch(name):
                resolved.add(name)
    return sorted(resolved)


def package_dependencies(package):
    """Read direct Depends and Pre-Depends from APT metadata."""
    proc = subprocess.run(
        ["apt-cache", "depends", "--important", package],
        capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not inspect dependencies for {package}")

    result = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        for prefix in ("Depends:", "PreDepends:"):
            if line.startswith(prefix):
                name = line.split(":", 1)[1].strip().lstrip("<").rstrip(">")
                name = name.split(":", 1)[0]
                if PACKAGE_NAME_RE.fullmatch(name):
                    result.add(name)
    return result


def full_dependency_closure(packages):
    """
    Build a recursive dependency closure from APT metadata. This deliberately
    includes dependencies already installed in the current RAM session.
    """
    resolved = set(packages)
    queue = list(packages)

    while queue:
        package = queue.pop(0)
        for dep in package_dependencies(package):
            if dep not in resolved:
                resolved.add(dep)
                queue.append(dep)

    # Also merge APT's solver result (alternatives/version choices).
    resolved.update(apt_dependency_closure(packages))
    return sorted(resolved)



def package_has_candidate(package):
    """
    Return True only for a real APT package with an installable candidate.

    Recursive dependency metadata can contain virtual package names such as
    debconf-2.0, mime-support or perlapi-*. Those names may satisfy Depends,
    but cannot themselves be downloaded with `apt-get download`.
    """
    proc = subprocess.run(
        ["apt-cache", "policy", package],
        capture_output=True,
        text=True,
        timeout=30,
        check=False
    )

    if proc.returncode != 0:
        return False

    match = re.search(
        r"^\s*Candidate:\s*(\S+)\s*$",
        proc.stdout,
        re.MULTILINE
    )

    return bool(
        match
        and match.group(1) not in ("(none)", "none")
    )

def package_is_installed(package):
    """
    True only when dpkg currently considers the package installed.
    Packages already supplied by the running MOS system are therefore
    not copied into /boot/optional/packages/.
    """
    proc = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
        capture_output=True,
        text=True,
        timeout=30,
        check=False
    )

    return (
        proc.returncode == 0
        and proc.stdout.strip().startswith("ii")
    )


def download_package_deb(package, destination, env):
    proc = subprocess.run(
        ["apt-get", "download", package],
        cwd=str(destination),
        input="y\n",
        capture_output=True, text=True, timeout=180,
        check=False, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Download failed for {package}: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[-1000:]
        )


def install_dependencies(tool):
    dependencies = get_dependencies(tool)
    if not dependencies:
        return get_dependency_status(tool)

    requested = sorted({
        dep["package"]
        for dep in dependencies
        if (
            dependency_is_required(tool, dep)
            and not dependency_is_installed(dep["command"])
            and dep["package"] != "__system__"
        )
    })

    if not requested:
        return get_dependency_status(tool)

    if not dependency_install_lock.acquire(blocking=False):
        raise RuntimeError("Another dependency installation is already running")

    cache_root = Path(f"/tmp/mos-tools-packages-{os.getpid()}")

    try:
        OPTIONAL_PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}

        console("DEPENDENCIES apt update -> " + ", ".join(requested))
        proc = subprocess.run(
            ["apt-get", "update"], input="y\n",
            capture_output=True, text=True,
            timeout=180, check=False, env=env
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "apt-get update failed: "
                + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[-1000:]
            )

        resolved = full_dependency_closure(requested)
        console("DEPENDENCIES resolved -> " + ", ".join(resolved))

        packages_to_persist = []

        for package in resolved:
            # IMPORTANT: recursive APT metadata can contain virtual dependency
            # names. They have no candidate version and must never reach
            # `apt-get download`.
            if not package_has_candidate(package):
                console(
                    f"DEPENDENCIES virtual/no candidate -> {package}"
                )

                # A package explicitly declared by a tool is expected to be a
                # real downloadable package. Fail clearly instead of silently
                # pretending the dependency was handled.
                if package in requested:
                    raise RuntimeError(
                        f"No installable APT candidate for requested package: {package}"
                    )

                continue

            if package in requested:
                # The command/module was missing, so its owning package must
                # become persistent even if dpkg metadata happens to claim
                # otherwise.
                packages_to_persist.append(package)
                continue

            if package_is_installed(package):
                console(
                    f"DEPENDENCIES system provided -> {package}"
                )
                continue

            packages_to_persist.append(package)

        console(
            "DEPENDENCIES persist set -> "
            + ", ".join(packages_to_persist)
        )

        for package in packages_to_persist:
            console(f"DEPENDENCIES download -> {package}")
            download_package_deb(package, cache_root, env)

        debs = sorted(cache_root.glob("*.deb"))
        if not debs:
            raise RuntimeError("APT did not download any package files")

        for deb in debs:
            shutil.copy2(deb, OPTIONAL_PACKAGE_ROOT / deb.name)

        console(f"DEPENDENCIES persisted {len(debs)} .deb file(s)")

        # Mirror MOS boot behaviour and install the entire persistent package set.
        persistent_debs = sorted(OPTIONAL_PACKAGE_ROOT.glob("*.deb"))
        last = None
        for _ in range(2):
            last = subprocess.run(
                ["dpkg", "-i", *[str(p) for p in persistent_debs]],
                capture_output=True, text=True, timeout=300,
                check=False, env=env
            )

        status = get_dependency_status(tool)
        missing = [
            x["command"]
            for x in status
            if x["required"] and not x["installed"]
        ]

        nonpersistent = [
            package
            for package in requested
            if package not in persistent_packages()
        ]

        if missing or nonpersistent:
            parts = []
            if missing:
                parts.append("missing commands: " + ", ".join(missing))
            if nonpersistent:
                parts.append("not persistent: " + ", ".join(nonpersistent))
            if last is not None and last.returncode != 0:
                detail = (last.stderr.strip() or last.stdout.strip())[-1000:]
                if detail:
                    parts.append("dpkg: " + detail)
            raise RuntimeError(
                "Dependency installation incomplete (" + "; ".join(parts) + ")"
            )

        return status

    finally:
        shutil.rmtree(cache_root, ignore_errors=True)
        dependency_install_lock.release()




def request_agent_restart():
    """
    Launch the external restart helper detached from this agent.

    The helper receives our PID and waits briefly before terminating us,
    allowing the current socket response to be sent cleanly first.
    """
    if not RESTART_HELPER.is_file():
        raise FileNotFoundError(
            f"Restart helper not found: {RESTART_HELPER}"
        )

    if not os.access(RESTART_HELPER, os.X_OK):
        raise PermissionError(
            f"Restart helper is not executable: {RESTART_HELPER}"
        )

    subprocess.Popen(
        [str(RESTART_HELPER), str(os.getpid())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True
    )

    console(f"RESTART requested for pid={os.getpid()}")

    return {
        "success": True,
        "message": "Agent restart requested",
        "pid": os.getpid()
    }


def handle_request(request):
    action = request.get("action")

    if action == "ping":
        console("PING")

        return {
            "success": True,
            "agent": "mos-tools-agent",
            "version": VERSION
        }

    if action == "restart":
        return request_agent_restart()

    if action == "list":
        tools = get_tools()

        console(
            f"LIST -> {sum(len(j) for j in tools.values())} "
            f"job(s) in {len(tools)} tool(s)"
        )

        return {
            "success": True,
            "tools": tools
        }

    if action == "run":
        tool = request.get("tool")
        job = request.get("job", "run")

        pid = run_job(tool, job)

        return {
            "success": True,
            "tool": tool,
            "job": job,
            "pid": pid
        }

    if action == "stop":
        tool = request.get("tool")
        job = request.get("job", "run")

        pid = stop_job(tool, job)

        return {
            "success": True,
            "tool": tool,
            "job": job,
            "pid": pid
        }

    if action == "status":
        tool = request.get("tool")
        job = request.get("job", "run")

        result = get_job_status(tool, job)

        console(
            f"STATUS {tool}/{job} -> "
            f"{'running' if result['running'] else 'idle'}"
        )

        return {
            "success": True,
            **result
        }

    if action == "get_meta":
        tool = request.get("tool")

        metadata = get_metadata(tool)

        console(f"META {tool}")

        return {
            "success": True,
            "tool": tool,
            "metadata": metadata
        }


    if action == "get_info":
        tool = request.get("tool")

        info = get_tool_info(tool)

        console(f"INFO {tool}")

        return {
            "success": True,
            "tool": tool,
            "info": info
        }


    if action == "get_config":
        tool = request.get("tool")

        config = parse_config(tool)

        console(f"CONFIG GET {tool}")

        return {
            "success": True,
            "tool": tool,
            "config": config
        }


    if action == "set_config":
        tool = request.get("tool")
        values = request.get("values")

        config = save_config(
            tool,
            values
        )

        return {
            "success": True,
            "tool": tool,
            "config": config
        }

    if action == "get_dependencies":
        tool = request.get("tool")
        dependencies = get_dependency_status(tool)
        console(f"DEPENDENCIES GET {tool}")
        return {
            "success": True,
            "tool": tool,
            "dependencies": dependencies
        }


    if action == "install_dependencies":
        tool = request.get("tool")
        dependencies = install_dependencies(tool)
        console(f"DEPENDENCIES INSTALL {tool}")
        return {
            "success": True,
            "tool": tool,
            "dependencies": dependencies
        }


    return {
        "success": False,
        "error": "Unknown action"
    }


# ------------------------------------------------------------
# Socket server
# ------------------------------------------------------------

def main():
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    STATUS_ROOT.mkdir(parents=True, exist_ok=True)

    os.makedirs(
        os.path.dirname(SOCKET_PATH),
        exist_ok=True
    )

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    server.bind(SOCKET_PATH)

    # DEVELOPMENT ONLY.
    # Vor dem produktiven Betrieb härten wir das.
    os.chmod(SOCKET_PATH, 0o666)

    server.listen(10)

    console(f"MOS Tools Agent v{VERSION}")
    console(f"Script root: {SCRIPT_ROOT}")
    console(f"Socket: {SOCKET_PATH}")
    console("Waiting for requests...")

    try:
        while True:
            connection, _ = server.accept()

            with connection:
                try:
                    raw = connection.recv(65536)

                    if not raw:
                        continue

                    request = json.loads(
                        raw.decode("utf-8")
                    )

                    result = handle_request(request)

                except Exception as exc:
                    console(
                        f"ERROR {type(exc).__name__}: {exc}"
                    )

                    result = {
                        "success": False,
                        "error": str(exc)
                    }

                send_response(connection, result)

    except KeyboardInterrupt:
        console("Stopping agent...")

    finally:
        server.close()

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        console("Agent stopped")


if __name__ == "__main__":
    main()
