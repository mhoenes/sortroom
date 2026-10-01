"""Background jobs (backfill, re-sort, maintenance …) for the API and the web UI.

Jobs live in memory until the process restarts. Each runs in its own thread under its mailbox's lock
(unless it needs none) and keeps the log lines it produced, so the UI can show what happened.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from datetime import datetime
from collections.abc import Callable
from typing import Any

from .config import Mailbox
from .runtime import single_instance

log = logging.getLogger(__name__)

MAX_JOBS = 50       # finished jobs kept
MAX_LOG_LINES = 400

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class _Capture(logging.Handler):
    """Hands each log record to the job whose thread wrote it."""

    def emit(self, record: logging.LogRecord) -> None:
        name = threading.current_thread().name
        if not name.startswith("job-"):
            return
        job = _jobs.get(name[4:])
        if job is None:
            return
        lines = job["log"]
        if len(lines) < MAX_LOG_LINES:
            lines.append(f"{datetime.fromtimestamp(record.created):%H:%M:%S} {record.levelname:<7} "
                         f"{record.getMessage()}")
        elif len(lines) == MAX_LOG_LINES:
            lines.append("… (more lines in logs/sortroom.log)")


_capture = _Capture(level=logging.INFO)
logging.getLogger("email_sorter").addHandler(_capture)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start(box: Mailbox, kind: str, label: str, fn: Callable[[], object], request: dict | None = None,
          needs_lock: bool = True) -> dict:
    """Run fn() in the background. fn returns a RunResult (or anything with as_dict(), or a dict)."""
    app_log = logging.getLogger("email_sorter")
    if app_log.getEffectiveLevel() > logging.INFO:  # the job log shows INFO lines even if the console doesn't
        app_log.setLevel(logging.INFO)
    job: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "mailbox": box.id, "kind": kind, "label": label,
                           "status": "running",
           "started": _now(), "finished": None, "request": request or {}, "result": None, "error": None, "log": []}
    with _lock:
        _jobs[job["id"]] = job
        for old in [j for j in _jobs.values() if j["status"] != "running"][:-MAX_JOBS]:
            _jobs.pop(old["id"], None)

    def work() -> None:
        try:
            if needs_lock:
                with single_instance(box.lock_path) as acquired:
                    if not acquired:
                        job.update(status="rejected", error="another run is active")
                        return
                    _execute(job, box, fn)
            else:
                _execute(job, box, fn)
        finally:
            job["finished"] = _now()

    # the job's thread inherits the request's context, e.g. the UI language of its messages
    threading.Thread(target=contextvars.copy_context().run, args=(work,), name=f"job-{job['id']}", daemon=True).start()
    return job


def _execute(job: dict, box: Mailbox, fn) -> None:
    log.info("[%s] starting %s", box.id, job["label"])
    try:
        result = fn()
        job.update(status="done", result=result.as_dict() if hasattr(result, "as_dict") else result)
    except Exception as e:
        log.exception("[%s] %s failed", box.id, job["label"])
        job.update(status="failed", error=str(e))


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def recent(box_id: str | None = None, limit: int = 10) -> list[dict]:
    jobs = [j for j in _jobs.values() if box_id is None or j["mailbox"] == box_id]
    return sorted(jobs, key=lambda j: j["started"], reverse=True)[:limit]


def running(box_id: str) -> bool:
    return any(j["mailbox"] == box_id and j["status"] == "running" for j in _jobs.values())


def rename_mailbox(old: str, new: str) -> None:
    """Keep the job list of a mailbox whose folder (id) was renamed."""
    for job in _jobs.values():
        if job["mailbox"] == old:
            job["mailbox"] = new


def remove_mailbox(box_id: str) -> None:
    """Forget the jobs of a deleted mailbox (a running one keeps going until it ends)."""
    with _lock:
        for job_id in [i for i, j in _jobs.items() if j["mailbox"] == box_id and j["status"] != "running"]:
            del _jobs[job_id]


def public(job: dict) -> dict:
    """The job as the API returns it (without the log)."""
    return {k: v for k, v in job.items() if k != "log"}
