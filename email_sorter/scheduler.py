"""Built-in schedule: every mailbox with [schedule] enabled gets a normal run every N minutes, and
with reconcile_enabled its log is reconciled with the mailbox every reconcile_hours (independent of `enabled`).
A mailbox with deletion rules has them applied once a day (CLEANUP_HOURS), also independent of `enabled`.

Runs in a background thread of the API/UI process. Each run takes the mailbox's lock like any
other run, so a run started by hand (or from outside via the API) is never doubled: the
scheduled one is skipped and tried again at its next slot. Settings are re-read on every tick,
so changes in the UI apply without a restart. When the log was last reconciled is kept in the
mailbox's state.db, so a restart doesn't cause an extra reconcile and one started by hand counts.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from collections.abc import Callable

from .config import ConfigError, Mailbox, imap_credentials, load_credentials
from .runtime import single_instance
from .sorter import run
from .store import last_cleanup, last_reconcile

log = logging.getLogger(__name__)

TICK_SECONDS = 20
FIRST_RUN_DELAY = timedelta(minutes=1)  # give the container a moment after (re)start
CLEANUP_HOURS = 24


def enabled_by_env() -> bool:
    """SORTROOM_SCHEDULER=off disables the schedule for this process (tests, one-off containers)."""
    return os.environ.get("SORTROOM_SCHEDULER", "on").lower() not in ("0", "off", "false", "no")


class Scheduler:
    def __init__(self, load_boxes: Callable[[], dict[str, Mailbox]],
                 run_box: Callable[[Mailbox], None] | None = None,
                 reconcile_box: Callable[[Mailbox], bool] | None = None,
                 last_reconciled: Callable[[Mailbox], datetime | None] | None = None,
                 cleanup_box: Callable[[Mailbox], bool] | None = None,
                 last_cleaned: Callable[[Mailbox], datetime | None] | None = None):
        self._load_boxes = load_boxes
        self._run_box = run_box or run_scheduled
        self._reconcile_box = reconcile_box or reconcile_scheduled
        self._last_reconciled = last_reconciled or (lambda box: last_reconcile(box.workspace))
        self.reconciled: dict[str, datetime | None] = {}  # mailbox id -> last reconcile, as last read
        self._cleanup_box = cleanup_box or cleanup_scheduled
        self._last_cleaned = last_cleaned or (lambda box: last_cleanup(box.workspace))
        self.cleaned: dict[str, datetime | None] = {}     # mailbox id -> deletion rules last applied
        self._first_seen: dict[str, datetime] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.next_run: dict[str, datetime] = {}   # mailbox id -> when its next run is due
        self.running: set[str] = set()
        self.started_at: datetime | None = None
        self.broken: dict[str, str] = {}  # mailbox id -> why its settings don't load, as last logged

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
        """Start the runs and reconciles that are due. Returns the ids started, a reconcile as
        "<id>:reconcile" (for tests)."""
        now = now or datetime.now()
        try:
            boxes = self._load_boxes()
        except ConfigError as e:
            log.error("schedule: configuration error, no runs: %s", e)
            return []
        self._note_broken(getattr(boxes, "broken", {}))
        started = []
        with self._lock:
            for box_id in list(self.next_run):
                if box_id not in boxes or not boxes[box_id].cfg.schedule_enabled:
                    del self.next_run[box_id]  # removed or switched off
            for box in boxes.values():
                if box.id in self.running:
                    continue
                if not box.cfg.schedule_enabled:
                    if self._reconcile_due(box, now):
                        started.append(self._start_reconcile(box, now))
                    elif self._cleanup_due(box, now):
                        started.append(self._start_cleanup(box, now))
                    continue
                interval = timedelta(minutes=box.cfg.schedule_minutes)
                due = self.next_run.get(box.id)
                if due is None:
                    due = self.next_run[box.id] = now + min(FIRST_RUN_DELAY, interval)
                elif due > now + interval:  # interval shortened in the UI
                    due = self.next_run[box.id] = now + interval
                if due > now:  # a normal run comes first, then the reconcile, then the deletion rules
                    if self._reconcile_due(box, now):
                        started.append(self._start_reconcile(box, now))
                    elif self._cleanup_due(box, now):
                        started.append(self._start_cleanup(box, now))
                    continue
                self.next_run[box.id] = now + interval
                self.running.add(box.id)
                started.append(box.id)
                threading.Thread(target=self._run, args=(box,), name=f"scheduled-{box.id}", daemon=True).start()
        return started

    def _note_broken(self, broken: dict[str, str]) -> None:
        for box_id, error in broken.items():
            if self.broken.get(box_id) != error:
                log.error("[%s] settings cannot be loaded, not sorted until fixed: %s", box_id, error)
        for box_id in set(self.broken) - set(broken):
            log.info("[%s] settings load again", box_id)
        self.broken = dict(broken)

    def _reconcile_due(self, box: Mailbox, now: datetime) -> bool:
        if not box.cfg.reconcile_enabled:
            return False
        hours = box.cfg.reconcile_hours
        first = self._first_seen.setdefault(box.id, now)
        last = self.reconciled.get(box.id)
        if last is not None and now < last + timedelta(hours=hours):
            return False
        last = self.reconciled[box.id] = self._last_reconciled(box)  # also counts one started by hand
        if last is None:
            return now >= first + FIRST_RUN_DELAY  # never reconciled: soon after startup
        return now >= last + timedelta(hours=hours)

    def _start_reconcile(self, box: Mailbox, now: datetime) -> str:
        self.reconciled[box.id] = now  # not again before the interval, unless it is skipped
        self.running.add(box.id)
        threading.Thread(target=self._reconcile, args=(box,), name=f"reconcile-{box.id}", daemon=True).start()
        return f"{box.id}:reconcile"

    def _reconcile(self, box: Mailbox) -> None:
        done = True
        try:
            done = self._reconcile_box(box)
        except Exception:
            log.exception("[%s] scheduled reconcile failed", box.id)
        finally:
            with self._lock:
                if not done:  # the mailbox was busy: try again at the next tick
                    self.reconciled.pop(box.id, None)
                self.running.discard(box.id)

    def _cleanup_due(self, box: Mailbox, now: datetime) -> bool:
        if not box.cfg.delete_rules:
            return False
        every = timedelta(hours=CLEANUP_HOURS)
        first = self._first_seen.setdefault(box.id, now)
        last = self.cleaned.get(box.id)
        if last is not None and now < last + every:
            return False
        last = self.cleaned[box.id] = self._last_cleaned(box)  # also counts a run by hand
        if last is None:
            return now >= first + FIRST_RUN_DELAY
        return now >= last + every

    def _start_cleanup(self, box: Mailbox, now: datetime) -> str:
        self.cleaned[box.id] = now
        self.running.add(box.id)
        threading.Thread(target=self._cleanup, args=(box,), name=f"cleanup-{box.id}", daemon=True).start()
        return f"{box.id}:cleanup"

    def _cleanup(self, box: Mailbox) -> None:
        done = True
        try:
            done = self._cleanup_box(box)
        except Exception:
            log.exception("[%s] scheduled deletion rules failed", box.id)
        finally:
            with self._lock:
                if not done:  # the mailbox was busy: try again at the next tick
                    self.cleaned.pop(box.id, None)
                self.running.discard(box.id)

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
                "active": self.active, "next": self.next_run.get(box.id), "running": box.id in self.running,
                **self._reconcile_status(box)}

    def _reconcile_status(self, box: Mailbox) -> dict:
        last = last_reconcile(box.workspace)
        due = last + timedelta(hours=box.cfg.reconcile_hours) if last else None
        return {"reconcile_enabled": box.cfg.reconcile_enabled, "last_reconcile": last, "next_reconcile": due}


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


def cleanup_scheduled(box: Mailbox) -> bool:
    """Apply the deletion rules for real. False when the mailbox was busy, so it is tried again soon."""
    from .cleanup import run_cleanup

    try:
        creds = imap_credentials(box)
    except ConfigError as e:
        log.error("[%s] scheduled deletion rules skipped: %s", box.id, e)
        return True
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            log.info("[%s] scheduled deletion rules postponed, another run is active", box.id)
            return False
        log.info("[%s] applying the deletion rules", box.id)
        result = run_cleanup(box.cfg, creds, box.workspace, live=True)
        log.info("[%s] deletion rules: %s", box.id, result["summary"])
    return True


def reconcile_scheduled(box: Mailbox) -> bool:
    """Reconcile the log with the mailbox for real (it changes only the log). False when the
    mailbox was busy, so it is tried again soon."""
    from .reconcile import run_reconcile

    try:
        creds = imap_credentials(box)  # only the IMAP login, the model isn't asked
    except ConfigError as e:
        log.error("[%s] scheduled reconcile skipped: %s", box.id, e)
        return True
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            log.info("[%s] scheduled reconcile postponed, another run is active", box.id)
            return False
        log.info("[%s] starting scheduled reconcile", box.id)
        result = run_reconcile(box.cfg, creds, box.workspace, live=True)
        log.info("[%s] reconcile: %s", box.id, result["summary"])
    return True
