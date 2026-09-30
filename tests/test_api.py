import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from email_sorter import api
from email_sorter.runtime import is_locked, single_instance
from email_sorter.config import Credentials, Mailbox
from email_sorter.sorter import RunResult
from support import example_config

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CFG = example_config()


def _boxes(tmp_path, ids):
    return {i: Mailbox(i, i.title(), tmp_path / i, CFG) for i in ids}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    calls = []

    def fake_run(cfg, creds, base_dir, live, limit):
        calls.append(("run", base_dir.name, live, limit))
        return RunResult(exit_code=0, live=live, classified=3, moved=2, categories={"werbung": 3})

    def fake_backfill(cfg, creds, base_dir, live, since, limit):
        calls.append(("backfill", base_dir.name, live, since, limit))
        return RunResult(exit_code=0, live=live, classified=10)

    monkeypatch.setattr(api, "run", fake_run)
    monkeypatch.setattr(api, "run_backfill", fake_backfill)
    monkeypatch.setattr(api, "_credentials", lambda box: Credentials("u", "p", "k"))

    clients = []

    def make(ids=("privat",)):
        boxes = _boxes(tmp_path, ids)
        monkeypatch.setattr(api, "load_mailboxes", lambda base, path: boxes)
        c = TestClient(api.app).__enter__()
        c.calls, c.boxes = calls, boxes
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok", "busy": False}


def test_run_requires_valid_token(client):
    assert client.post("/run").status_code == 401
    assert client.post("/run", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_run_refuses_when_token_not_configured(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "short")
    assert client.post("/run", headers={"Authorization": "Bearer short"}).status_code == 500


def test_run_returns_summary_per_mailbox_and_defaults_to_live(client):
    r = client.post("/run", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["results"]["privat"]["classified"] == 3
    assert client.calls == [("run", "privat", True, None)]
    assert not is_locked(client.boxes["privat"].lock_path)  # released afterwards


def test_run_covers_all_mailboxes(make_client):
    c = make_client(("gmail", "privat"))
    body = c.post("/run", headers=AUTH, json={"live": False, "limit": 5}).json()
    assert set(body["results"]) == {"gmail", "privat"}
    assert c.calls == [("run", "gmail", False, 5), ("run", "privat", False, 5)]


def test_run_one_mailbox(make_client):
    c = make_client(("gmail", "privat"))
    c.post("/run", headers=AUTH, json={"mailbox": "gmail"})
    assert c.calls == [("run", "gmail", True, None)]
    assert c.post("/run", headers=AUTH, json={"mailbox": "nope"}).status_code == 404


def test_busy_mailbox_is_skipped_others_still_run(make_client):
    c = make_client(("gmail", "privat"))
    with single_instance(c.boxes["privat"].lock_path):  # a run holds it
        assert c.get("/mailboxes", headers=AUTH).json()[1] == {"id": "privat", "name": "Privat", "busy": True}
        body = c.post("/run", headers=AUTH).json()
        assert body["ok"] and body["results"]["privat"]["skipped"]
        assert c.calls == [("run", "gmail", True, None)]
        assert c.post("/run", headers=AUTH, json={"mailbox": "privat"}).status_code == 409
        assert c.get("/health").json()["busy"] is True
    assert c.get("/health").json()["busy"] is False  # released with its run


def test_mailboxes_listing(make_client):
    c = make_client(("gmail", "privat"))
    assert c.get("/mailboxes", headers=AUTH).json() == [
        {"id": "gmail", "name": "Gmail", "busy": False}, {"id": "privat", "name": "Privat", "busy": False}]


def test_backfill_runs_in_background_and_reports_via_job(client, monkeypatch):
    from email_sorter import i18n
    monkeypatch.setattr(i18n, "DEFAULT_LANGUAGE", "en")
    r = client.post("/backfill", headers=AUTH, json={"since": "2025-01-01", "live": True})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(50):
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["classified"] == 10 and job["mailbox"] == "privat"
    assert client.calls == [("backfill", "privat", True, date(2025, 1, 1), None)]
    assert job["label"] == "Backfill since 1 Jan 2025"  # in the UI language, like the maintenance page's own


def test_backfill_needs_mailbox_when_several(make_client):
    c = make_client(("gmail", "privat"))
    assert c.post("/backfill", headers=AUTH, json={"since": "2025-01-01"}).status_code == 422
    assert c.post("/backfill", headers=AUTH, json={"since": "2025-01-01", "mailbox": "gmail"}).status_code == 202


def test_backfill_defaults_to_dry_run(client):
    r = client.post("/backfill", headers=AUTH, json={"since": "2025-01-01"})
    assert r.json()["request"]["live"] is False


def test_backfill_rejects_future_date(client):
    future = (date.today() + timedelta(days=3)).isoformat()
    assert client.post("/backfill", headers=AUTH, json={"since": future}).status_code == 422


def test_unknown_job_is_404(client):
    assert client.get("/jobs/nope", headers=AUTH).status_code == 404


def test_old_pid_lock_files_are_removed_on_startup(tmp_path, monkeypatch):
    boxes = _boxes(tmp_path, ("gmail", "privat"))
    for b in boxes.values():
        (b.workspace / "data").mkdir(parents=True)
        (b.workspace / "data" / "run.lock").write_text("1")  # up to 0.13.1
    monkeypatch.setattr(api, "load_mailboxes", lambda base, path: boxes)
    with TestClient(api.app):
        assert not any((b.workspace / "data" / "run.lock").exists() for b in boxes.values())
