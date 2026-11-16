"""Process-level helpers shared by the CLI and the HTTP API: paths, logging, run lock, keep-awake."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

log = logging.getLogger("email_sorter")



# Set when the process is shutting down (the container was stopped): runs stop asking the model, apply what they
# have classified and write it to the log, instead of being cut off in the middle of moving mail.
_stop = threading.Event()


def request_stop() -> None:
    _stop.set()


def reset_stop() -> None:
    _stop.clear()


def stopping() -> bool:
    return _stop.is_set()


def sleep_unless_stopping(seconds: float) -> bool:
    """Sleep, but not through a shutdown. True when it was cut short."""
    return _stop.wait(seconds)


def wait_idle(lock_paths: list[Path], timeout: float) -> bool:
    """Wait until none of the run locks is held, at most `timeout` seconds. Whether they all are free."""
    deadline = time.monotonic() + timeout
    while any(is_locked(p) for p in lock_paths):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def default_config_path() -> Path:
    """The shared config: SORTROOM_CONFIG if set, else config/config.toml."""
    return Path(os.environ.get("SORTROOM_CONFIG") or BASE_DIR / "config" / "config.toml")


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


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive(lock_path: Path, timeout: float = 30):
    """Wait for the lock, then hold it - for short work like rewriting a file, which a thread or process
    doing the same would otherwise undo. Raises TimeoutError after `timeout` seconds."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{lock_path} stayed locked for {timeout:g} s")
            time.sleep(0.01)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def is_locked(lock_path: Path) -> bool:
    """Whether a run holds the lock now. Takes it for a moment to find out, so a run starting at that
    very moment is skipped once and comes again at its next slot."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)


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
    """Skip this run if another one is still going (scheduled runs can overlap).

    An OS file lock (flock; on Windows msvcrt.locking): it holds against other threads, processes and
    containers sharing the folder, and the system releases it when its process ends - a crashed run
    leaves nothing behind. The file itself stays; removing it could let two runs lock two files."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            yield False
            return
        try:
            yield True
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
