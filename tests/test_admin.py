import re
import time
import tomllib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from email_sorter import api, jobs, manual, web
from email_sorter.config import load_mailboxes
from email_sorter.classifier import Decision
from email_sorter.sorter import RunResult
from email_sorter.store import Store
from email_sorter.web import admin
from email_sorter.web.editing import (EditError, rename_category_key, rename_folder_refs, save_sender_rules)

from test_editing import MAILBOX, PASSWORD, SHARED

ENV = {"IMAP_USER": "u", "IMAP_PASSWORD": "p", "CLASSIFIER_API_KEY": "k"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    box_dir = tmp_path / "mailboxes" / "privat"
    box_dir.mkdir(parents=True)
    (box_dir / "mailbox.toml").write_text(MAILBOX.replace(
        'folder = "INBOX/Werbung"', 'folder = "INBOX/Werbung"\nexpired_folder = "INBOX/Werbung/Alt"'), encoding="utf-8")
    store = Store(box_dir / "data" / "state.db")
    store.record(SimpleNamespace(key="<m1@x>", received="2026-09-20T10:00+02:00", sender="Shop@Example.de",
                                 subject="Unsicher", decision=Decision("werbung", 0.4, {"werbung": 0.4}, 0.1, 0.0001),
                                 folder=None, flag=False, expires=None, source="classifier"))
    store.close()
    return tmp_path


def _box(base):
    return load_mailboxes(base, base / "config.toml")["privat"]


def _wait(job_id):
    deadline = time.monotonic() + 5
    while jobs.get(job_id)["status"] == "running" and time.monotonic() < deadline:
        pass
    return jobs.get(job_id)


# ---------------------------------------------------------------- jobs

def test_job_captures_its_log_and_result(setup):
    box = _box(setup)
    logger = __import__("logging").getLogger("email_sorter.test")

    def work():
        logger.info("hallo aus dem Job")
        return RunResult(exit_code=0, live=True, classified=3)

    job = _wait(jobs.start(box, "run", "Lauf", work)["id"])
    assert job["status"] == "done" and job["result"]["classified"] == 3
    assert any("hallo aus dem Job" in line for line in job["log"])
    assert "log" not in jobs.public(job)


def test_job_rejected_when_mailbox_locked(setup):
    box = _box(setup)
    box.lock_path.parent.mkdir(parents=True, exist_ok=True)
    box.lock_path.write_text(str(__import__("os").getpid()))
    try:
        job = _wait(jobs.start(box, "run", "Lauf", lambda: RunResult(exit_code=0))["id"])
        assert job["status"] == "rejected"
    finally:
        box.lock_path.unlink()


# ---------------------------------------------------------------- manual + editing helpers

def test_move_mail_to_category_without_folder_needs_no_imap(setup, monkeypatch):
    box = _box(setup)
    monkeypatch.setattr(manual, "MailBox", None)  # would fail if IMAP were touched
    save = dict(box.cfg.categories)
    assert manual.target_for(box.cfg, "inbox") is None
    with pytest.raises(manual.ManualError):
        manual.target_for(box.cfg, "gibtsnicht")
    assert manual.move_mail(box.cfg, SimpleNamespace(), box.workspace, "<m1@x>", "inbox") is None
    row = Store(box.workspace / "data" / "state.db").get("<m1@x>")
    assert row["source"] == "manual" and row["confidence"] == 1.0 and row["moved_to"] is None
    assert save == box.cfg.categories


def test_rename_folder_refs(setup):
    box = _box(setup)
    assert rename_folder_refs(box, setup / "config.toml", "INBOX/Werbung", "INBOX/Angebote") == 2
    cat = _box(setup).cfg.categories["werbung"]
    assert cat.folder == "INBOX/Angebote" and cat.expired_folder == "INBOX/Angebote/Alt"
    assert _box(setup).cfg.categories["finanzen"].folder == "INBOX/Finanzen"


def test_rename_category_key_updates_rules(setup):
    shared = setup / "config.toml"
    save_sender_rules(_box(setup), shared, [("@shop.de", "werbung")])
    rename_category_key(_box(setup), shared, "werbung", "angebote")
    cfg = _box(setup).cfg
    assert list(cfg.categories) == ["finanzen", "angebote"] and cfg.categories["angebote"].label == "Werbung"
    assert cfg.sender_rules[0].action == "angebote"
    with pytest.raises(EditError, match="gibt es schon"):
        rename_category_key(_box(setup), shared, "angebote", "finanzen")


# ---------------------------------------------------------------- pages

@pytest.fixture
def client(setup, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    monkeypatch.setattr(api, "BASE_DIR", setup)
    monkeypatch.setattr(api, "CONFIG_PATH", setup / "config.toml")
    monkeypatch.setattr(api.app.state, "config_path", setup / "config.toml")
    monkeypatch.setattr(api.app.state, "base_dir", setup)
    monkeypatch.setattr(api.app.state, "load_mailboxes", lambda: load_mailboxes(setup, setup / "config.toml"))
    with TestClient(api.app) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_maintenance_page_and_run_job(client, monkeypatch):
    calls = []

    def fake_run(cfg, creds, ws, live, limit):
        calls.append((live, limit))
        return RunResult(exit_code=0, live=live, classified=5, moved=4)

    monkeypatch.setattr(admin, "run", fake_run)
    html = client.get("/ui/m/privat/maintenance").text
    assert "Wartung · Privat" in html and "IMAP_PASSWORD" in html and "gesetzt" in html
    r = client.post("/ui/m/privat/maintenance/run", data={"csrf": _csrf(html), "limit": "20"}, follow_redirects=False)
    assert r.status_code == 303 and "/ui/m/privat/jobs/" in r.headers["location"]
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert calls == [(False, 20)]
    html = client.get(r.headers["location"]).text
    assert "Lauf (Probelauf)" in html and "Würde verschieben" in html
    assert "Lauf (Probelauf)" in client.get("/ui/m/privat/maintenance").text  # listed as recent job
    # "Ausführen" is the second submit button of each task; it posts live=1
    page = client.get("/ui/m/privat/maintenance").text
    assert page.count('>Probelauf</button>') == page.count('name="live" value="1"') == 8
    r = client.post("/ui/m/privat/maintenance/run", data={"csrf": _csrf(page), "live": "1"}, follow_redirects=False)
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert calls[-1] == (True, None)
    assert "Jetzt ausführen" not in client.get(r.headers["location"]).text  # already live


def test_dry_run_can_be_started_for_real_from_its_job_page(client, monkeypatch):
    calls = []

    def fake_run(cfg, creds, ws, live, limit):
        calls.append((live, limit))
        return RunResult(exit_code=0, live=live, classified=5, moved=4)

    monkeypatch.setattr(admin, "run", fake_run)
    token = _csrf(client.get("/ui/m/privat/maintenance").text)
    r = client.post("/ui/m/privat/maintenance/run", data={"csrf": token, "limit": "20"}, follow_redirects=False)
    _wait(r.headers["location"].rsplit("/", 1)[1])
    html = client.get(r.headers["location"]).text
    assert "Probelauf abgeschlossen" in html and 'action="/ui/m/privat/maintenance/run"' in html
    assert '<input type="hidden" name="limit" value="20">' in html
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', html.split('class="card rerun"')[1]))
    r = client.post("/ui/m/privat/maintenance/run", data={**form, "live": "1"}, follow_redirects=False)
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert calls == [(False, 20), (True, 20)]  # same limit, now for real


def test_no_live_rerun_after_failed_dry_run(client, monkeypatch):
    monkeypatch.setattr(admin, "run", lambda cfg, creds, ws, live, limit: RunResult(exit_code=1, live=live))
    token = _csrf(client.get("/ui/m/privat/maintenance").text)
    r = client.post("/ui/m/privat/maintenance/run", data={"csrf": token}, follow_redirects=False)
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert "Jetzt ausführen" not in client.get(r.headers["location"]).text


def test_maintenance_validation_errors(client):
    token = _csrf(client.get("/ui/m/privat/maintenance").text)
    r = client.post("/ui/m/privat/maintenance/backfill", data={"csrf": token, "since": "2999-01-01"})
    assert "Zukunft" in r.text
    r = client.post("/ui/m/privat/maintenance/rename_category", data={"csrf": token, "old": "werbung",
                                                                       "new": "finanzen"})
    assert "schon vergeben" in r.text
    assert client.post("/ui/m/privat/maintenance/nix", data={"csrf": token}).status_code == 404


def test_mail_actions(client, setup, monkeypatch):
    moves = []
    monkeypatch.setattr(admin, "move_mail", lambda cfg, creds, ws, key, cat: moves.append((key, cat)) or "INBOX/Werbung")
    html = client.get("/ui/m/privat/mails?period=all&key=%3Cm1%40x%3E").text
    assert "Übernehmen: nach Werbung" in html and "alle von @example.de" in html
    assert ">Werbung</option>" in html and "Werbung → Werbung" not in html  # target named once
    token = _csrf(html)
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "accept",
                                                       "category": "werbung", "back": "period=all"})
    assert "Verschoben nach INBOX/Werbung" in r.text and moves == [("<m1@x>", "werbung")]
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "rule",
                                                       "match": "@example.de", "category": "werbung"})
    assert "Absender-Regel für @example.de gespeichert" in r.text and len(moves) == 1
    assert [(x.match, x.action) for x in _box(setup).cfg.sender_rules][-1] == ("@example.de", "werbung")


def test_add_mailbox(client, setup):
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Gmail", "id": "gmail", "imap_host": "imap.gmail.com", "imap_port": "993",
            "source_folder": "INBOX", "user_env": "gmail_user", "password_env": "GMAIL_PASSWORD", "template": "privat"}
    r = client.post("/ui/mailboxes/new", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/gmail/maintenance"
    raw = tomllib.loads((setup / "mailboxes" / "gmail" / "mailbox.toml").read_text(encoding="utf-8"))
    assert raw["imap"]["user_env"] == "GMAIL_USER" and set(raw["categories"]) == {"finanzen", "werbung"}
    html = client.get(r.headers["location"]).text
    assert "Postfach angelegt" in html and "fehlt in .env" in html
    r = client.post("/ui/mailboxes/new", data=form)
    assert r.status_code == 422 and "gibt es schon" in r.text


def test_shared_settings(client, setup):
    html = client.get("/ui/settings").text
    assert "example/model-1" in html and "CLASSIFIER_API_KEY" in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-2",
            "max_body_chars": "4000", "timeout_seconds": "30",
            "min_interval_seconds": "0,5"}
    r = client.post("/ui/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    cfg = _box(setup).cfg
    assert cfg.classifier_model == "example/model-2" and cfg.max_body_chars == 4000 and cfg.min_interval_seconds == 0.5
    r = client.post("/ui/settings", data={**form, "endpoint": "http://unsicher"})
    assert r.status_code == 422 and "https" in r.text


def test_set_expiry_by_hand(client, setup):
    html = client.get("/ui/m/privat/mails?period=all&key=%3Cm1%40x%3E").text
    token = _csrf(html)
    assert 'name="expires"' in html and "Werbung/Alt" in html  # help names the category's expired folder
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "expiry",
                                                       "expires": "2020-01-31"})
    assert "Gültig bis 31.01.2020 gespeichert" in r.text and "INBOX/Werbung/Alt" in r.text
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    assert store.get("<m1@x>")["expires"] == "2020-01-31"
    store.close()
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "expiry",
                                                       "clear": "1"})
    assert "Ablaufdatum entfernt" in r.text
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "expiry",
                                                       "expires": "kein-datum"})
    assert "gültiges Datum" in r.text


def test_add_gmail_mailbox_uses_top_level_labels(client, setup):
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Gmail", "id": "gmail", "imap_host": "imap.gmail.com", "imap_port": "993",
            "source_folder": "INBOX", "user_env": "GMAIL_USER", "password_env": "GMAIL_PASSWORD", "template": "privat"}
    assert client.post("/ui/mailboxes/new", data=form, follow_redirects=False).status_code == 303
    raw = tomllib.loads((setup / "mailboxes" / "gmail" / "mailbox.toml").read_text(encoding="utf-8"))
    assert raw["categories"]["werbung"]["folder"] == "Werbung"
    assert raw["categories"]["werbung"]["expired_folder"] == "Werbung/Alt"
    assert raw["categories"]["finanzen"]["folder"] == "Finanzen"
    privat = tomllib.loads((setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8"))
    assert privat["categories"]["werbung"]["folder"] == "INBOX/Werbung"  # the template is untouched


def test_first_mailbox_from_the_example(client, setup):
    import shutil

    shutil.rmtree(setup / "mailboxes")
    assert "Noch kein Postfach" in client.get("/ui").text
    html = client.get("/ui/mailboxes/new").text
    assert 'value="_example" selected' in html and "Standard-Kategorien" in html
    form = {"csrf": _csrf(html), "name": "Privat", "id": "privat", "imap_host": "imap.strato.de", "imap_port": "993",
            "source_folder": "INBOX", "user_env": "IMAP_USER", "password_env": "IMAP_PASSWORD", "template": "_example"}
    assert client.post("/ui/mailboxes/new", data=form, follow_redirects=False).status_code == 303
    box = _box(setup)
    assert box.name == "Privat" and "werbung" in box.cfg.categories and box.cfg.schedule_enabled
    assert box.cfg.sender_rules == ()
