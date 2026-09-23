import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from email_sorter import api
from email_sorter.config import Credentials, load_config
from email_sorter.sorter import RunResult

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CFG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    lock = tmp_path / "run.lock"
    monkeypatch.setattr(api, "LOCK_PATH", lock)
    monkeypatch.setattr(api, "_load", lambda: (CFG, Credentials("u", "p", "k")))
    calls = []

    def fake_run(cfg, creds, base_dir, live, limit):
        calls.append(("run", live, limit))
        return RunResult(exit_code=0, live=live, classified=3, moved=2, categories={"newsletter": 3})

    def fake_backfill(cfg, creds, base_dir, live, since, limit):
        calls.append(("backfill", live, since, limit))
        return RunResult(exit_code=0, live=live, classified=10)

    monkeypatch.setattr(api, "run", fake_run)
    monkeypatch.setattr(api, "run_backfill", fake_backfill)
    with TestClient(api.app) as c:
        c.calls, c.lock = calls, lock
        yield c


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok", "busy": False}


def test_run_requires_valid_token(client):
    assert client.post("/run").status_code == 401
    assert client.post("/run", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_run_refuses_when_token_not_configured(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "short")
    assert client.post("/run", headers={"Authorization": "Bearer short"}).status_code == 500


def test_run_returns_summary_and_defaults_to_live(client):
    r = client.post("/run", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["classified"] == 3 and body["categories"] == {"newsletter": 3}
    assert client.calls == [("run", True, None)]
    assert not client.lock.exists()  # released afterwards


def test_run_dry_with_limit(client):
    client.post("/run", headers=AUTH, json={"live": False, "limit": 5})
    assert client.calls == [("run", False, 5)]


def test_run_rejected_while_another_run_holds_the_lock(client):
    client.lock.write_text(str(os.getpid()))  # a live process holds it
    assert client.post("/run", headers=AUTH).status_code == 409
    assert client.get("/health").json()["busy"] is True


def test_backfill_runs_in_background_and_reports_via_job(client):
    r = client.post("/backfill", headers=AUTH, json={"since": "2025-01-01", "live": True})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(50):
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["classified"] == 10
    assert client.calls == [("backfill", True, date(2025, 1, 1), None)]


def test_backfill_defaults_to_dry_run(client):
    r = client.post("/backfill", headers=AUTH, json={"since": "2025-01-01"})
    assert r.json()["request"]["live"] is False


def test_backfill_rejects_future_date(client):
    future = (date.today() + timedelta(days=3)).isoformat()
    assert client.post("/backfill", headers=AUTH, json={"since": future}).status_code == 422


def test_unknown_job_is_404(client):
    assert client.get("/jobs/nope", headers=AUTH).status_code == 404


def test_stale_lock_from_previous_container_is_removed_on_startup(tmp_path, monkeypatch):
    lock = tmp_path / "run.lock"
    lock.write_text("1")  # PID 1 is "alive" in a container, but belonged to the crashed process
    monkeypatch.setattr(api, "LOCK_PATH", lock)
    with TestClient(api.app):
        assert not lock.exists()
