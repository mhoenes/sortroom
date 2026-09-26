import os
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from email_sorter import scheduler
from email_sorter.config import ConfigError, _read_toml, config_from_raw, load_config
from email_sorter.runtime import BASE_DIR

T0 = datetime(2026, 9, 26, 12, 0)


def _box(box_id, enabled=True, minutes=10, tmp=None):
    cfg = SimpleNamespace(schedule_enabled=enabled, schedule_minutes=minutes)
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
    box.cfg, box.workspace = load_config(BASE_DIR / "config.toml"), tmp_path
    monkeypatch.setattr(scheduler, "load_credentials", lambda cfg: "creds")
    runs = []
    monkeypatch.setattr(scheduler, "run", lambda *a, **kw: runs.append(kw))
    box.lock_path.parent.mkdir(parents=True)
    box.lock_path.write_text(str(os.getpid()))              # a live process holds the lock
    scheduler.run_scheduled(box)
    assert runs == []
    box.lock_path.unlink()
    scheduler.run_scheduled(box)
    assert runs == [{"live": True, "limit": None}]


def test_schedule_settings_in_config():
    raw = _read_toml(BASE_DIR / "config.toml")
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
