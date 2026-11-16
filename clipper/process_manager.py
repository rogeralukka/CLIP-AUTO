"""Process tracking, line-by-line output streaming, cancellation, pausing, and per-job logging."""
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger("clipper.process_manager")

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

_LOCK = threading.Lock()
# Mapping job_id -> set of active Popen objects
_ACTIVE_POPE_PROCESSES: Dict[str, Set[subprocess.Popen]] = {}
# Mapping job_id -> threading.Event for cancellation
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
# Mapping job_id -> threading.Event for pausing
_PAUSE_EVENTS: Dict[str, threading.Event] = {}


class JobCancelledException(Exception):
    """Raised when a running job is cancelled by the user."""
    pass


class JobPausedException(Exception):
    """Raised when a running job is paused by the user."""
    pass


def register_job(job_id: str):
    """Register a new job for cancellation and pause tracking."""
    with _LOCK:
        if job_id not in _CANCEL_EVENTS:
            _CANCEL_EVENTS[job_id] = threading.Event()
        if job_id not in _PAUSE_EVENTS:
            _PAUSE_EVENTS[job_id] = threading.Event()
        if job_id not in _ACTIVE_POPE_PROCESSES:
            _ACTIVE_POPE_PROCESSES[job_id] = set()


def log_job_message(job_id: Optional[str], message: str):
    """Append a timestamped log message to a specific job's log file."""
    if not job_id:
        return
    log_file = os.path.join(LOGS_DIR, f"{job_id}.log")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_msg = message.rstrip("\r\n")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {clean_msg}\n")
    except Exception:
        pass


def get_job_logs(job_id: str, max_lines: int = 30) -> List[str]:
    """Retrieve the last N lines from a specific job's log file."""
    log_file = os.path.join(LOGS_DIR, f"{job_id}.log")
    if not os.path.isfile(log_file):
        return []
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return lines[-max_lines:]
    except Exception:
        return []


def is_job_cancelled(job_id: Optional[str]) -> bool:
    """Check if cancellation has been requested for a job."""
    if not job_id:
        return False
    with _LOCK:
        event = _CANCEL_EVENTS.get(job_id)
        return event.is_set() if event else False


def is_job_paused(job_id: Optional[str]) -> bool:
    """Check if pause has been requested for a job."""
    if not job_id:
        return False
    with _LOCK:
        event = _PAUSE_EVENTS.get(job_id)
        return event.is_set() if event else False


def check_cancelled(job_id: Optional[str]):
    """Raise JobCancelledException if job has been cancelled."""
    if is_job_cancelled(job_id):
        raise JobCancelledException(f"Job {job_id} was cancelled by user.")


def check_cancelled_or_paused(job_id: Optional[str]):
    """Check both cancellation and pause status, raising appropriate exception."""
    if is_job_cancelled(job_id):
        raise JobCancelledException(f"Job {job_id} was cancelled by user.")
    if is_job_paused(job_id):
        raise JobPausedException(f"Job {job_id} was paused by user.")


def register_process(job_id: Optional[str], proc: subprocess.Popen):
    """Associate a running subprocess with a job."""
    if not job_id:
        return
    with _LOCK:
        if job_id in _ACTIVE_POPE_PROCESSES:
            _ACTIVE_POPE_PROCESSES[job_id].add(proc)


def unregister_process(job_id: Optional[str], proc: subprocess.Popen):
    """Remove a finished subprocess from tracking."""
    if not job_id:
        return
    with _LOCK:
        if job_id in _ACTIVE_POPE_PROCESSES:
            _ACTIVE_POPE_PROCESSES[job_id].discard(proc)


def pause_job_processes(job_id: str):
    """Set pause event and terminate running subprocesses to free hardware resources."""
    log_job_message(job_id, "PAUSE requested by user. Terminating active subprocesses...")
    with _LOCK:
        if job_id not in _PAUSE_EVENTS:
            _PAUSE_EVENTS[job_id] = threading.Event()
        _PAUSE_EVENTS[job_id].set()
        procs = list(_ACTIVE_POPE_PROCESSES.get(job_id, []))

    for proc in procs:
        try:
            pid = proc.pid
            logger.info(f"Suspending/terminating process PID {pid} for paused Job {job_id}...")
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5
                )
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            else:
                proc.kill()
        except Exception as e:
            logger.warning(f"Error terminating process PID {getattr(proc, 'pid', None)}: {e}")

    with _LOCK:
        if job_id in _ACTIVE_POPE_PROCESSES:
            _ACTIVE_POPE_PROCESSES[job_id].clear()


def resume_job_event(job_id: str):
    """Clear pause event so the job can resume execution."""
    log_job_message(job_id, "RESUME requested by user.")
    with _LOCK:
        if job_id in _PAUSE_EVENTS:
            _PAUSE_EVENTS[job_id].clear()


def cancel_job_processes(job_id: str):
    """Set cancel event and forcibly kill all subprocess trees for this job."""
    log_job_message(job_id, "CANCEL requested by user. Terminating all active subprocesses...")
    with _LOCK:
        if job_id not in _CANCEL_EVENTS:
            _CANCEL_EVENTS[job_id] = threading.Event()
        _CANCEL_EVENTS[job_id].set()
        procs = list(_ACTIVE_POPE_PROCESSES.get(job_id, []))

    for proc in procs:
        try:
            pid = proc.pid
            logger.info(f"Terminating process tree for PID {pid} (Job {job_id})...")
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5
                )
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            else:
                proc.kill()
        except Exception as e:
            logger.warning(f"Error terminating process PID {getattr(proc, 'pid', None)}: {e}")

    with _LOCK:
        if job_id in _ACTIVE_POPE_PROCESSES:
            _ACTIVE_POPE_PROCESSES[job_id].clear()


def run_tracked_command(
    cmd: list,
    job_id: Optional[str] = None,
    cwd: Optional[str] = None,
    line_callback: Optional[Callable[[str], None]] = None,
    timeout: Optional[float] = None,
) -> int:
    """Execute a command while tracking PID, checking cancellation/pause, and streaming per-job output."""
    check_cancelled_or_paused(job_id)

    cmd_str = " ".join(str(c) for c in cmd)
    logger.debug(f"Running command: {cmd_str}")
    log_job_message(job_id, f"Executing: {cmd_str}")

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    register_process(job_id, proc)
    start_time = time.time()

    try:
        if proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                # Per-job log line writing
                clean_line = line.rstrip("\r\n")
                if job_id:
                    log_job_message(job_id, clean_line)

                if is_job_cancelled(job_id):
                    cancel_job_processes(job_id)
                    raise JobCancelledException(f"Job {job_id} cancelled.")

                if is_job_paused(job_id):
                    pause_job_processes(job_id)
                    raise JobPausedException(f"Job {job_id} paused.")

                if line_callback:
                    try:
                        line_callback(clean_line)
                    except Exception:
                        pass

                if timeout and (time.time() - start_time) > timeout:
                    proc.kill()
                    raise TimeoutError(f"Command timed out after {timeout}s")

        proc.wait(timeout=5)
        ret = proc.returncode
    except Exception as e:
        try:
            if sys.platform == "win32" and proc.pid:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.kill()
        except Exception:
            pass
        raise e
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        unregister_process(job_id, proc)

    check_cancelled_or_paused(job_id)
    return ret
