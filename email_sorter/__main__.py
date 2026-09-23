"""Command line entry point: python -m email_sorter [--live] [--limit N] [--check]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, load_config, load_credentials

BASE_DIR = Path(__file__).resolve().parent.parent
STALE_LOCK_SECONDS = 3600

log = logging.getLogger("email_sorter")


def setup_logging(verbose: bool) -> None:
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        BASE_DIR / "logs" / "email-sorter.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
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
    """Stale if the owning process is gone (killed run) or the lock is very old."""
    if time.time() - lock_path.stat().st_mtime > STALE_LOCK_SECONDS:
        return True
    try:
        return not _pid_alive(int(lock_path.read_text().strip()))
    except (OSError, ValueError):
        return False


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_sorter", description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="actually move/flag mails (default is a dry run that only writes a CSV report)")
    parser.add_argument("--limit", type=int, help="classify at most N mails this run")
    parser.add_argument("--check", action="store_true",
                        help="test IMAP login and the Jev connection, list folders, change nothing")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(BASE_DIR / ".env")
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
        creds = load_credentials(cfg)
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2

    if args.check:
        from .check import check
        return check(cfg, creds)

    from .sorter import run

    with single_instance(BASE_DIR / "data" / "run.lock") as acquired:
        if not acquired:
            log.info("another run is still active, skipping")
            return 0
        log.info("starting %s run", "LIVE" if args.live else "dry")
        try:
            return run(cfg, creds, BASE_DIR, live=args.live, limit=args.limit)
        except Exception:
            log.exception("run failed")
            return 1


if __name__ == "__main__":
    sys.exit(main())
