"""Built-in schedule: every mailbox with [schedule] enabled gets a normal run every N minutes.

Runs in a background thread of the API/UI process. Each run takes the mailbox's lock like any
other run, so a run started by hand (or from outside via the API) is never doubled: the
scheduled one is skipped and tried again at its next slot. Settings are re-read on every tick,
so changes in the UI apply without a restart.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Callable

from .config import ConfigError, Mailbox, load_credentials
from .runtime import single_instance
from .sorter import run

log = logging.getLogger(__name__)

TICK_SECONDS = 20
FIRST_RUN_DELAY = timedelta(minutes=1)  # give the container a moment after (re)start


def enabled_by_env() -> bool:
    """SORTROOM_SCHEDULER=off disables the schedule for this process (tests, one-off containers)."""
    return os.environ.get("SORTROOM_SCHEDULER", "on").lower() not in ("0", "off", "false", "no")


class Scheduler:
    def __init__(self, load_boxes: Callable[[], dict[str, Mailbox]],
                 run_box: Callable[[Mailbox], None] | None = None):
        self._load_boxes = load_boxes
        self._run_box = run_box or run_scheduled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.next_run: dict[str, datetime] = {}   # mailbox id -> when its next run is due
        self.running: set[str] = set()
        self.started_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.started_at = datetime.now()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        log.info("schedule started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("schedule tick failed")
            self._stop.wait(TICK_SECONDS)

    # ------------------------------------------------------------ work
    def tick(self, now: datetime | None = None) -> list[str]:
        """Start the runs that are due. Returns the ids started (for tests)."""
        now = now or datetime.now()
        try:
            boxes = self._load_boxes()
        except ConfigError as e:
            log.error("schedule: configuration error, no runs: %s", e)
            return []
        started = []
        with self._lock:
            for box_id in list(self.next_run):
                if box_id not in boxes or not boxes[box_id].cfg.schedule_enabled:
                    del self.next_run[box_id]  # removed or switched off
            for box in boxes.values():
                if not box.cfg.schedule_enabled:
                    continue
                interval = timedelta(minutes=box.cfg.schedule_minutes)
                due = self.next_run.get(box.id)
                if due is None:
                    due = self.next_run[box.id] = now + min(FIRST_RUN_DELAY, interval)
                elif due > now + interval:  # interval shortened in the UI
                    due = self.next_run[box.id] = now + interval
                if due > now or box.id in self.running:
                    continue
                self.next_run[box.id] = now + interval
                self.running.add(box.id)
                started.append(box.id)
                threading.Thread(target=self._run, args=(box,), name=f"scheduled-{box.id}", daemon=True).start()
        return started

    def _run(self, box: Mailbox) -> None:
        try:
            self._run_box(box)
        except Exception:
            log.exception("[%s] scheduled run failed", box.id)
        finally:
            with self._lock:
                self.running.discard(box.id)

    def status(self, box: Mailbox) -> dict:
        """What the UI shows about a mailbox's schedule."""
        return {"enabled": box.cfg.schedule_enabled, "minutes": box.cfg.schedule_minutes,
                "active": self.active, "next": self.next_run.get(box.id), "running": box.id in self.running}


def run_scheduled(box: Mailbox) -> None:
    """A normal live run, the same as POST /run, skipped when the mailbox is busy."""
    try:
        creds = load_credentials(box)
    except ConfigError as e:
        log.error("[%s] scheduled run skipped: %s", box.id, e)
        return
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            log.info("[%s] scheduled run skipped, another run is active", box.id)
            return
        log.info("[%s] starting scheduled run", box.id)
        run(box.cfg, creds, box.workspace, live=True, limit=None)
