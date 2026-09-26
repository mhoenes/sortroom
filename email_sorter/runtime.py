"""Process-level helpers shared by the CLI and the HTTP API: paths, logging, run lock, keep-awake."""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STALE_LOCK_SECONDS = 3600

log = logging.getLogger("email_sorter")

LOCK_PATH = BASE_DIR / "data" / "run.lock"  # the single-mailbox location; each Mailbox has its own lock_path


def setup_logging(verbose: bool) -> None:
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        BASE_DIR / "logs" / "sortroom.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:  # None under pythonw.exe (scheduled task)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(lock_path: Path) -> bool:
    """Stale if the owning process is gone (killed run). A long backfill can run for
    hours, so age only decides when the lock holds no readable PID."""
    try:
        return not _pid_alive(int(lock_path.read_text().strip()))
    except (OSError, ValueError):
        return time.time() - lock_path.stat().st_mtime > STALE_LOCK_SECONDS


@contextmanager
def keep_awake():
    """Stop Windows from going to standby while a run is active (a backfill can take an hour)."""
    if os.name != "nt":
        yield
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@contextmanager
def single_instance(lock_path: Path):
    """Skip this run if another one is still going (scheduled runs can overlap)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() and _lock_is_stale(lock_path):
        log.info("removing stale lock from an interrupted run")
        lock_path.unlink(missing_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield True
    finally:
        lock_path.unlink(missing_ok=True)
