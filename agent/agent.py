#!/usr/bin/env python3

import stat
import errno
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


VERSION = "1.13.3"

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


def get_log(tool, job, lines=200):
    # Validate tool/job against an actual executable job.
    get_script(tool, job)

    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 200

    # Keep responses bounded even if a client sends nonsense.
    lines = max(1, min(lines, 2000))

    path = log_file(tool, job)

    if not path.is_file():
        return {
            "tool": tool,
            "job": job,
            "lines": [],
            "line_count": 0,
            "requested_lines": lines
        }

    # Logs are expected to be modest, but only retain the requested
    # tail in memory.
    from collections import deque

    with open(
        path,
        "r",
        encoding="utf-8",
        errors="replace"
    ) as f:
        tail = list(deque(f, maxlen=lines))

    tail = [
        line.rstrip("\n")
        for line in tail
    ]

    return {
        "tool": tool,
        "job": job,
        "lines": tail,
        "line_count": len(tail),
        "requested_lines": lines
    }




# ------------------------------------------------------------
# Dynamic collections
# ------------------------------------------------------------

def get_collections_path(tool):
    return get_tool_dir(safe_name(tool)) / "collections.json"


def load_collections(tool):
    path = get_collections_path(tool)
    if not path.is_file():
        return {"jobs": []}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("collections.json must contain an object")

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("collections.jobs must be an array")

    return {"jobs": jobs}


def validate_collections(tool, data):
    if not isinstance(data, dict):
        raise ValueError("Collections data must be an object")

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("jobs must be an array")
    if len(jobs) > 100:
        raise ValueError("Maximum 100 jobs")

    result = []
    seen = set()

    for item in jobs:
        if not isinstance(item, dict):
            raise ValueError("Each job must be an object")

        job_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        targets = item.get("targets", [])

        if not job_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
            raise ValueError(f"Invalid job id: {job_id!r}")
        if job_id in seen:
            raise ValueError(f"Duplicate job id: {job_id}")
        seen.add(job_id)

        if not name or len(name) > 128 or "\n" in name or "\r" in name:
            raise ValueError(f"{job_id}: invalid job name")

        if not isinstance(targets, list) or not targets:
            raise ValueError(f"{job_id}: at least one target is required")
        if len(targets) > 100:
            raise ValueError(f"{job_id}: maximum 100 targets")

        clean_targets = []
        target_seen = set()
        for target in targets:
            target = str(target).strip()
            if (
                not target
                or len(target) > 1024
                or "\n" in target
                or "\r" in target
                or not target.startswith("/")
            ):
                raise ValueError(f"{job_id}: invalid target {target!r}")
            if target not in target_seen:
                clean_targets.append(target)
                target_seen.add(target)

        result.append({
            "id": job_id,
            "name": name,
            "targets": clean_targets
        })

    return {"jobs": result}


def save_collections(tool, data):
    clean = validate_collections(tool, data)
    path = get_collections_path(tool)
    temp = path.with_name(path.name + ".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, path)
    return clean



# ------------------------------------------------------------
# File Integrity findings / manual accept
# ------------------------------------------------------------

def integrity_findings_path(tool, job):
    tool = safe_name(tool)
    job = safe_name(job)
    return get_tool_dir(tool) / "findings" / f"{job}.json"


def get_integrity_findings(tool, job):
    # Validates that the dynamic job still exists.
    definition = get_job_definition(tool, job)
    path = integrity_findings_path(tool, job)
    if not path.is_file():
        return {
            "job": job,
            "name": definition.get("name", job),
            "updated": None,
            "findings": []
        }
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("findings", []), list):
        raise ValueError("Invalid findings file")
    return data


def _path_belongs_to_job(path, targets):
    candidate = Path(path).resolve(strict=True)
    for target in targets:
        root = Path(target).resolve(strict=True)
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            pass
    raise ValueError("Finding path is outside this job's targets")


def _stable_signature(path):
    st = path.stat()
    return (
        st.st_dev, st.st_ino, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns
    )


integrity_accept_lock = threading.Lock()


def _accept_integrity_finding_locked(tool, job, path_value):
    if tool != "file-integrity":
        raise ValueError("Accept is only available for file-integrity")

    definition = get_job_definition(tool, job)
    targets = definition.get("targets") or []

    if not isinstance(path_value, str) or not path_value.startswith("/"):
        raise ValueError("Invalid finding path")

    # Accept is only allowed for an unresolved finding.
    # Check this BEFORE hashing or touching xattrs.
    finding_path = path_value
    queue_before = get_integrity_findings(tool, job)

    if not any(
        item.get("path") == finding_path
        for item in queue_before.get("findings", [])
    ):
        raise ValueError("Finding is not present in findings queue")

    path = _path_belongs_to_job(path_value, targets)
    if not path.is_file():
        raise ValueError("Finding path is not a regular file")

    config = parse_config(tool)
    nice_value = int(config.get("NICE", 10))
    ionice_class = int(config.get("IONICE_CLASS", 2))
    ionice_level = int(config.get("IONICE_LEVEL", 7))

    if not 0 <= nice_value <= 19:
        raise ValueError("NICE must be between 0 and 19")

    if ionice_class not in (1, 2, 3):
        raise ValueError("IONICE_CLASS must be 1, 2 or 3")

    if not 0 <= ionice_level <= 7:
        raise ValueError("IONICE_LEVEL must be between 0 and 7")

    hash_cmd = [
        "nice", "-n", str(nice_value),
        "ionice", "-c", str(ionice_class),
    ]

    # ionice idle class (3) has no priority level.
    if ionice_class in (1, 2):
        hash_cmd += ["-n", str(ionice_level)]

    hash_cmd += [
        "b3sum", "--num-threads=1", "--no-mmap",
    ]

    # Open the file once and keep that exact inode for hashing and xattr
    # updates. O_NOFOLLOW prevents a final-component symlink swap.
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(str(path), flags)
    try:
        st_before = os.fstat(fd)
        if not stat.S_ISREG(st_before.st_mode):
            raise ValueError("Finding path is not a regular file")

        content_sig_before = (
            st_before.st_dev,
            st_before.st_ino,
            st_before.st_size,
            st_before.st_mtime_ns,
        )

        proc = subprocess.run(
            hash_cmd + [f"/proc/self/fd/{fd}"],
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            timeout=None,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "b3sum failed")

        current_hash = proc.stdout.split()[0].strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", current_hash):
            raise RuntimeError("Invalid BLAKE3 result")

        st_after_hash = os.fstat(fd)
        content_sig_after_hash = (
            st_after_hash.st_dev,
            st_after_hash.st_ino,
            st_after_hash.st_size,
            st_after_hash.st_mtime_ns,
        )
        if content_sig_before != content_sig_after_hash:
            raise RuntimeError("File changed while Accept was hashing it")

        scandate = str(int(time.time())).encode("ascii")
        mtime = str(int(st_after_hash.st_mtime)).encode("ascii")
        size = str(st_after_hash.st_size).encode("ascii")
        new_values = {
            "user.blake3": current_hash.encode("ascii"),
            "user.scandate": scandate,
            "user.filedate": mtime,
            "user.filesize": size,
        }

        # Snapshot the complete old reference set so a failed Accept can be
        # rolled back instead of leaving a half-updated baseline.
        old_values = {}
        for attr in new_values:
            try:
                old_values[attr] = os.getxattr(fd, attr)
            except OSError as exc:
                # ENODATA is 61 on Linux; use getattr for portability.
                if exc.errno == getattr(errno, "ENODATA", 61):
                    old_values[attr] = None
                else:
                    raise

        def rollback_xattrs():
            rollback_errors = []
            for attr, old_value in old_values.items():
                try:
                    if old_value is None:
                        try:
                            os.removexattr(fd, attr)
                        except OSError as exc:
                            if exc.errno != getattr(errno, "ENODATA", 61):
                                raise
                    else:
                        os.setxattr(fd, attr, old_value)
                except Exception as exc:
                    rollback_errors.append(f"{attr}: {exc}")
            return rollback_errors

        try:
            # Metadata first, reference hash last. The rollback snapshot above
            # protects against partial writes.
            for attr in ("user.scandate", "user.filedate", "user.filesize"):
                os.setxattr(fd, attr, new_values[attr])

            # Ensure file data did not change between hashing and committing.
            st_before_commit = os.fstat(fd)
            commit_sig = (
                st_before_commit.st_dev,
                st_before_commit.st_ino,
                st_before_commit.st_size,
                st_before_commit.st_mtime_ns,
            )
            if commit_sig != content_sig_after_hash:
                raise RuntimeError("File changed before Accept could commit")

            os.setxattr(fd, "user.blake3", new_values["user.blake3"])

            # xattr writes change ctime, so only compare inode/data properties.
            st_after_commit = os.fstat(fd)
            final_sig = (
                st_after_commit.st_dev,
                st_after_commit.st_ino,
                st_after_commit.st_size,
                st_after_commit.st_mtime_ns,
            )
            if final_sig != content_sig_after_hash:
                raise RuntimeError("File changed while Accept was committing")

        except Exception as exc:
            rollback_errors = rollback_xattrs()
            if rollback_errors:
                raise RuntimeError(
                    f"{exc}; xattr rollback incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise

    finally:
        os.close(fd)

    # Remove only the exact finding that the user accepted, and only after
    # hashing + complete xattr commit succeeded.
    data = get_integrity_findings(tool, job)
    before_count = len(data.get("findings", []))
    data["findings"] = [
        item for item in data.get("findings", [])
        if item.get("path") != finding_path
    ]
    if len(data["findings"]) == before_count:
        raise RuntimeError("Accepted file was not present in findings queue")

    data["updated"] = timestamp()

    findings_path = integrity_findings_path(tool, job)
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    temp = findings_path.with_name(findings_path.name + ".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, findings_path)

    return {
        "path": finding_path,
        "hash": current_hash,
        "accepted": True,
    }


def accept_integrity_finding(tool, job, path_value):
    # Prevent two simultaneous Accept requests from racing the queue.
    with integrity_accept_lock:
        return _accept_integrity_finding_locked(
            tool,
            job,
            path_value
        )


# ------------------------------------------------------------
# Tool / Job discovery
# ------------------------------------------------------------

def get_dynamic_jobs(tool):
    """Return configured dynamic jobs for a tool.

    v1.12 supports stable collection-backed jobs:

      "dynamic_jobs": {
        "collection": "jobs",
        "script": "scan.sh"
      }

    Legacy count/prefix mode remains supported for older test/tools.
    """
    tool = safe_name(tool)
    tool_dir = get_tool_dir(tool)
    metadata = get_raw_metadata(tool)
    spec = metadata.get("dynamic_jobs")

    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ValueError("dynamic_jobs must be an object")

    script_name = spec.get("script")
    if not isinstance(script_name, str) or not script_name.endswith(".sh"):
        raise ValueError("dynamic_jobs.script must name a .sh file")

    script = (tool_dir / script_name).resolve()
    try:
        script.relative_to(tool_dir)
    except ValueError:
        raise ValueError("Dynamic job script outside allowed tool directory")
    if not script.is_file():
        raise FileNotFoundError(f"Dynamic job script not found: {script_name}")
    if not os.access(script, os.X_OK):
        raise PermissionError(f"{tool}/{script_name} is not executable")

    collection = spec.get("collection")
    if collection is not None:
        if collection != "jobs":
            raise ValueError("Only dynamic_jobs.collection='jobs' is supported")

        result = {}
        for index, item in enumerate(load_collections(tool)["jobs"], 1):
            job_id = safe_name(str(item["id"]))
            result[job_id] = {
                "script": script,
                "args": [job_id],
                "name": item["name"],
                "targets": list(item["targets"]),
                "index": index,
            }
        return result

    # Legacy v1.11 count-based dynamic jobs.
    count_key = spec.get("count")
    prefix = spec.get("prefix", "JOB")
    name_field = spec.get("name_field", "NAME")

    if not isinstance(count_key, str) or not count_key:
        raise ValueError("dynamic_jobs.count must be a configuration key")
    if not isinstance(prefix, str) or not prefix or not all(c.isalnum() or c == "_" for c in prefix):
        raise ValueError("dynamic_jobs.prefix is invalid")
    if not isinstance(name_field, str) or not name_field or not all(c.isalnum() or c == "_" for c in name_field):
        raise ValueError("dynamic_jobs.name_field is invalid")

    config = parse_config(tool)
    try:
        count = int(config.get(count_key, 0))
    except (TypeError, ValueError):
        raise ValueError(f"{count_key}: value must be an integer")
    if count < 0 or count > 100:
        raise ValueError(f"{count_key}: dynamic job count out of range")

    result = {}
    config_prefix = prefix.upper()
    for index in range(1, count + 1):
        job_id = f"job_{index}"
        name_key = f"{config_prefix}_{index}_{name_field.upper()}"
        display_name = str(config.get(name_key, "")).strip() or f"Job {index}"
        result[job_id] = {
            "script": script,
            "args": [job_id],
            "name": display_name,
            "index": index,
        }
    return result

def get_job_definition(tool, job):
    """Resolve a job to executable + argv without changing legacy behaviour."""
    tool = safe_name(tool)
    job = safe_name(job)
    tool_dir = get_tool_dir(tool)

    dynamic = get_dynamic_jobs(tool)
    if job in dynamic:
        return dynamic[job]

    script = (tool_dir / f"{job}.sh").resolve()
    try:
        script.relative_to(tool_dir)
    except ValueError:
        raise ValueError("Script outside allowed tool directory")
    if not script.is_file():
        raise FileNotFoundError(f"Job '{job}' not found for tool '{tool}'")
    if not os.access(script, os.X_OK):
        raise PermissionError(f"{tool}/{job}.sh is not executable")

    return {
        "script": script,
        "args": [],
        "name": job,
        "index": None,
    }


def get_script(tool, job):
    # Compatibility helper used by log/status validation and existing callers.
    return get_job_definition(tool, job)["script"]


def get_tools():
    result = {}

    if not SCRIPT_ROOT.is_dir():
        return result

    for tool_dir in sorted(SCRIPT_ROOT.iterdir()):
        if not tool_dir.is_dir() or tool_dir.is_symlink():
            continue

        jobs = []

        # Legacy/static jobs remain unchanged. A script declared as the generic
        # dynamic runner is infrastructure, not a separately schedulable job.
        dynamic_runner = None
        metadata_path = tool_dir / "tool.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                spec = metadata.get("dynamic_jobs") if isinstance(metadata, dict) else None
                if isinstance(spec, dict):
                    dynamic_runner = spec.get("script")
            except Exception:
                # Metadata/config errors are surfaced below by get_dynamic_jobs;
                # do not silently turn a broken dynamic runner into a static job.
                pass

        for script in sorted(tool_dir.glob("*.sh")):
            if not script.is_file() or script.is_symlink():
                continue
            try:
                resolved = script.resolve()
                resolved.relative_to(tool_dir.resolve())
            except ValueError:
                continue
            if dynamic_runner and script.name == dynamic_runner:
                continue
            jobs.append(script.stem)

        dynamic_jobs = get_dynamic_jobs(tool_dir.name)
        jobs.extend(dynamic_jobs.keys())

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
    definition = get_job_definition(tool, job)
    script = definition["script"]
    args = definition.get("args", [])
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
            [str(script), *args],
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

    status = read_status(tool, job)

    # Persistierter "running"-Status kann nach Agent-Neustart veraltet sein.
    if status and status.get("running"):
        pid = status.get("pid")

        try:
            pid = int(pid)
        except (TypeError, ValueError):
            pid = None

        if not pid or not os.path.exists(f"/proc/{pid}"):
            status["running"] = False
            status["finished"] = timestamp()
            status["exit_code"] = None
            status["duration"] = None

            write_status(tool, job, status)

    return status

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


def get_raw_metadata(tool):
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



def get_metadata(tool):
    """
    Return effective tool metadata.

    Static jobs from tool.json are preserved.  Configured dynamic jobs are
    synthesized into metadata.jobs so existing WebUI pages (dashboard,
    settings/schedules and logs) can consume them without special handling.
    """
    metadata = get_raw_metadata(tool)
    result = dict(metadata)

    jobs = result.get("jobs", {})
    if jobs is None:
        jobs = {}
    if not isinstance(jobs, dict):
        raise ValueError("jobs must be an object")

    jobs = dict(jobs)

    dynamic = get_dynamic_jobs(tool)
    for job_id, definition in dynamic.items():
        if job_id in jobs:
            raise ValueError(
                f"Dynamic job '{job_id}' conflicts with a static job"
            )

        jobs[job_id] = {
            "name": definition.get("name") or job_id,
            "description": definition.get("description")
                or "Dynamic job",
            "targets": definition.get("targets", [])
        }

    result["jobs"] = jobs
    return result


def get_config_schema(tool, values=None):
    """
    Build the complete configuration schema.

    Repeated sections are expanded from their repeat counter.  When
    saving, submitted values take precedence over the value currently
    stored in config.conf so reading and writing use exactly the same
    schema logic.
    """
    metadata = get_raw_metadata(tool)

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
        # Values are written inside double quotes. Semicolon is therefore
        # safe as data and is required for lists such as EXCLUDES.
        # Reject characters that can escape/expand the quoted assignment.
        forbidden = [
            "\n",
            "\r",
            "`",
            "$",
            "\\",
            '"',
            "'",
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

    # Build the schema using the submitted values.
    # This is important for dynamic/repeated sections whose size
    # depends on values such as mod_count.
    schema = get_config_schema(tool, values)

    if not schema:
        raise ValueError(
            f"Tool '{tool}' has no configurable fields"
        )

    # Start from existing/default config so partial updates work.
    config = parse_config(tool)

    # The existing config may have been parsed using the old repeat
    # count. Remove fields which no longer exist in the new schema.
    #
    # Example:
    #   mod_count: 2 -> 1
    #   mod_2_* must disappear from the resulting configuration.
    config = {
        key: value
        for key, value in config.items()
        if key in schema
    }

    # Add defaults for fields which are new in the resulting schema.
    #
    # Example:
    #   mod_count: 1 -> 2
    #   mod_2_* receives the defaults from tool.json.
    for key, field in schema.items():

        if key not in config and "default" in field:
            config[key] = field["default"]

    # Apply submitted values.
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
        metadata = get_raw_metadata(tool)

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

    if action == "get_log":
        tool = request.get("tool")
        job = request.get("job")
        lines = request.get("lines", 200)

        result = get_log(
            tool,
            job,
            lines
        )

        return {
            "success": True,
            **result
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


    if action == "get_collections":
        tool = request.get("tool")
        collections = load_collections(tool)
        return {
            "success": True,
            "tool": tool,
            "collections": collections
        }

    if action == "set_collections":
        tool = request.get("tool")
        values = request.get("collections")
        collections = save_collections(tool, values)
        console(f"COLLECTIONS SET {tool}")
        return {
            "success": True,
            "tool": tool,
            "collections": collections
        }

    if action == "get_findings":
        tool = request.get("tool")
        job = request.get("job")
        return {
            "success": True,
            "tool": tool,
            "job": job,
            "data": get_integrity_findings(tool, job)
        }

    if action == "accept_finding":
        tool = request.get("tool")
        job = request.get("job")
        path = request.get("path")
        result = accept_integrity_finding(tool, job, path)
        console(f"INTEGRITY ACCEPT {tool}/{job} {result['path']}")
        return {
            "success": True,
            "tool": tool,
            "job": job,
            "result": result
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
