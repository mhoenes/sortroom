import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from email_sorter import scheduler
from email_sorter.config import EXAMPLE_MAILBOXES, ConfigError, _read_toml, config_from_raw
from email_sorter.runtime import BASE_DIR, single_instance
from email_sorter.sorter import RunResult
from support import example_config

T0 = datetime(2026, 9, 26, 12, 0)


def _box(box_id, enabled=True, minutes=10, tmp=None):
    cfg = SimpleNamespace(schedule_enabled=enabled, schedule_minutes=minutes, reconcile_enabled=False, reconcile_hours=24,
                          delete_rules=())
    return SimpleNamespace(id=box_id, cfg=cfg, lock_path=(tmp / box_id / "run.lock") if tmp else None)


class Recorder:
    def __init__(self):
        self.calls, self.done = [], threading.Event()

    def __call__(self, box):
        self.calls.append(box.id)
        self.done.set()


def _wait_idle(s):
    for _ in range(200):
        if not s.running:
            return
        threading.Event().wait(0.01)


def test_first_run_after_a_short_delay_then_every_interval():
    boxes = {"privat": _box("privat", minutes=10)}
    rec = Recorder()
    s = scheduler.Scheduler(lambda: boxes, rec)
    assert s.tick(T0) == []                                   # scheduled, not yet due
    assert s.next_run["privat"] == T0 + scheduler.FIRST_RUN_DELAY
    assert s.tick(T0 + timedelta(seconds=59)) == []
    assert s.tick(T0 + timedelta(minutes=1)) == ["privat"]
    _wait_idle(s)
    assert rec.calls == ["privat"] and s.next_run["privat"] == T0 + timedelta(minutes=11)
    assert s.tick(T0 + timedelta(minutes=10)) == []
    assert s.tick(T0 + timedelta(minutes=11)) == ["privat"]


def test_disabled_mailboxes_are_not_run_and_forgotten():
    boxes = {"privat": _box("privat"), "gmail": _box("gmail", enabled=False)}
    s = scheduler.Scheduler(lambda: boxes, Recorder())
    s.tick(T0)
    assert set(s.next_run) == {"privat"}
    boxes["privat"].cfg.schedule_enabled = False
    s.tick(T0 + timedelta(minutes=5))
    assert s.next_run == {}


def test_shortened_interval_applies_at_once():
    boxes = {"privat": _box("privat", minutes=60)}
    s = scheduler.Scheduler(lambda: boxes, Recorder())
    s.next_run["privat"] = T0 + timedelta(minutes=50)
    boxes["privat"].cfg.schedule_minutes = 5
    s.tick(T0)
    assert s.next_run["privat"] == T0 + timedelta(minutes=5)


def test_a_running_mailbox_is_not_started_twice():
    boxes = {"privat": _box("privat", minutes=1)}
    release = threading.Event()
    calls = []

    def slow(box):
        calls.append(box.id)
        release.wait(5)

    s = scheduler.Scheduler(lambda: boxes, slow)
    s.next_run["privat"] = T0
    assert s.tick(T0) == ["privat"]
    assert s.tick(T0 + timedelta(minutes=5)) == []           # still running
    release.set()
    _wait_idle(s)
    assert calls == ["privat"]


def test_config_error_means_no_runs():
    def broken():
        raise ConfigError("kaputt")

    assert scheduler.Scheduler(broken, Recorder()).tick(T0) == []


def test_scheduled_run_skips_a_busy_mailbox(tmp_path, monkeypatch):
    box = _box("privat", tmp=tmp_path)
    box.cfg, box.workspace = example_config(), tmp_path
    monkeypatch.setattr(scheduler, "load_credentials", lambda cfg: "creds")
    runs = []
    monkeypatch.setattr(scheduler, "run", lambda *a, **kw: runs.append(kw) or result)
    result = RunResult(exit_code=0)
    with single_instance(box.lock_path):                      # a run holds the lock
        assert scheduler.run_scheduled(box) is None           # skipped: nothing to report
    assert runs == []
    assert scheduler.run_scheduled(box) == (False, "")
    assert runs == [{"live": True, "limit": None}]
    result = RunResult(exit_code=2, error="IMAP login failed")
    assert scheduler.run_scheduled(box) == (True, "IMAP login failed")
    result = RunResult(exit_code=1, failed=1)                  # one mail failed: retried, no alarm
    assert scheduler.run_scheduled(box) == (False, "")


def test_schedule_settings_in_config():
    raw = {**_read_toml(EXAMPLE_MAILBOXES["de"]), "classifier": _read_toml(BASE_DIR / "config" / "config.toml")["classifier"]}
    cfg = config_from_raw(raw, "x")
    assert cfg.schedule_enabled and cfg.schedule_minutes == 10          # on by default
    cfg = config_from_raw({**raw, "schedule": {"enabled": False, "interval_minutes": 30}}, "x")
    assert not cfg.schedule_enabled and cfg.schedule_minutes == 30
    with pytest.raises(ConfigError, match="interval_minutes"):
        config_from_raw({**raw, "schedule": {"interval_minutes": 0}}, "x")


def test_env_switch(monkeypatch):
    monkeypatch.setenv("SORTROOM_SCHEDULER", "off")
    assert not scheduler.enabled_by_env()
    monkeypatch.setenv("SORTROOM_SCHEDULER", "on")
    assert scheduler.enabled_by_env()


def test_app_starts_and_stops_the_schedule(monkeypatch):
    from fastapi.testclient import TestClient

    from email_sorter import api

    calls = []
    fake = SimpleNamespace(start=lambda: calls.append("start"), stop=lambda: calls.append("stop"))
    monkeypatch.setattr(api.app.state, "scheduler", fake)
    monkeypatch.setattr(api, "load_mailboxes", lambda base, path: {})
    monkeypatch.setenv("SORTROOM_SCHEDULER", "on")
    with TestClient(api.app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]
    calls.clear()
    monkeypatch.setenv("SORTROOM_SCHEDULER", "off")
    with TestClient(api.app):
        pass
    assert calls == ["stop"]


# ---------------------------------------------------------------- reconcile

def _reconciler(result=True):
    calls = []

    def reconcile(box):
        calls.append(box.id)
        return result
    return calls, reconcile


def test_reconcile_once_a_day_after_the_last_one():
    box = _box("privat", enabled=False)
    box.cfg.reconcile_enabled = True
    last = {"privat": None}
    calls, reconcile = _reconciler()
    s = scheduler.Scheduler(lambda: {"privat": box}, Recorder(), reconcile, lambda b: last[b.id])
    assert s.tick(T0) == []                                   # never reconciled: soon after startup
    assert s.tick(T0 + scheduler.FIRST_RUN_DELAY) == ["privat:reconcile"]
    _wait_idle(s)
    assert calls == ["privat"]
    last["privat"] = T0 + scheduler.FIRST_RUN_DELAY            # what the reconcile wrote into state.db
    assert s.tick(T0 + timedelta(hours=23)) == []
    last["privat"] = T0 + timedelta(hours=20)                   # started by hand meanwhile: counts too
    assert s.tick(T0 + timedelta(hours=25)) == []
    assert s.tick(T0 + timedelta(hours=44)) == ["privat:reconcile"]
    box.cfg.reconcile_enabled = False                           # switched off
    assert s.tick(T0 + timedelta(days=5)) == []


def test_deletion_rules_once_a_day_and_only_with_rules():
    box = _box("privat", enabled=False)
    last = {"privat": None}
    calls, cleanup = _reconciler()
    s = scheduler.Scheduler(lambda: {"privat": box}, Recorder(), lambda b: True, lambda b: None,
                            cleanup_box=cleanup, last_cleaned=lambda b: last[b.id])
    assert s.tick(T0 + timedelta(hours=1)) == []                 # no rules: nothing to do
    box.cfg.delete_rules = ("a rule",)
    assert s.tick(T0 + timedelta(hours=2)) == []                 # rules just added: a moment later
    assert s.tick(T0 + timedelta(hours=2) + scheduler.FIRST_RUN_DELAY) == ["privat:cleanup"]
    _wait_idle(s)
    assert calls == ["privat"]
    last["privat"] = T0 + timedelta(hours=2)
    assert s.tick(T0 + timedelta(hours=25)) == []
    assert s.tick(T0 + timedelta(hours=27)) == ["privat:cleanup"]
    _wait_idle(s)
    box.cfg.reconcile_enabled = True                              # both due: the reconcile first
    s.cleaned.clear()
    last["privat"] = T0
    assert s.tick(T0 + timedelta(days=3)) == ["privat:reconcile"]


def test_a_busy_reconcile_is_retried_and_a_run_goes_first():
    box = _box("privat", minutes=10)
    box.cfg.reconcile_enabled = True
    calls, reconcile = _reconciler(result=False)                # mailbox busy
    rec = Recorder()
    s = scheduler.Scheduler(lambda: {"privat": box}, rec, reconcile, lambda b: T0 - timedelta(days=2))
    assert s.tick(T0) == ["privat:reconcile"]                   # the run is not due before the first delay
    _wait_idle(s)
    assert s.tick(T0 + timedelta(seconds=20)) == ["privat:reconcile"]  # skipped, so tried again
    _wait_idle(s)
    assert s.tick(T0 + scheduler.FIRST_RUN_DELAY) == ["privat"]  # both due: the normal run first
    _wait_idle(s)
    assert rec.calls == ["privat"] and calls == ["privat", "privat"]


def test_last_reconcile_is_kept_in_the_log(tmp_path):
    from email_sorter.store import Store, last_reconcile
    assert last_reconcile(tmp_path) is None                     # no log yet
    store = Store(tmp_path / "data" / "state.db")
    assert last_reconcile(tmp_path) is None
    store.set_meta("last_reconcile", "2026-09-27T03:00:00")
    store.close()
    assert last_reconcile(tmp_path) == datetime(2026, 9, 27, 3, 0)


def test_reconcile_hours_in_the_settings(tmp_path):
    raw = _read_toml(EXAMPLE_MAILBOXES["en"])
    raw["classifier"] = {"endpoint": "https://x.invalid", "model": "m"}
    cfg = config_from_raw(raw, "x")
    assert cfg.reconcile_enabled and cfg.reconcile_hours == 24
    raw["schedule"]["reconcile_hours"] = 0
    with pytest.raises(ConfigError, match="reconcile_hours"):
        config_from_raw(raw, "x")


def test_broken_mailboxes_are_logged_once_and_the_others_run(caplog):
    from email_sorter.config import Mailboxes
    boxes = Mailboxes({"privat": _box("privat")}, {"arbeit": "bad toml"})
    ran = []
    s = scheduler.Scheduler(lambda: boxes, run_box=lambda box: ran.append(box.id), reconcile_box=lambda box: True,
                  last_reconciled=lambda box: None)
    with caplog.at_level("INFO", logger="email_sorter.scheduler"):
        s.tick(T0)
        s.tick(T0 + timedelta(seconds=20))
        boxes.broken.clear()
        s.tick(T0 + timedelta(seconds=40))
    messages = [r.getMessage() for r in caplog.records]
    assert sum("[arbeit] settings cannot be loaded" in m for m in messages) == 1
    assert any("[arbeit] settings load again" in m for m in messages)
