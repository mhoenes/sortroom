import logging
import re
import time
from datetime import datetime
import tomllib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from email_sorter import api, imap, jobs, manual
from email_sorter.config import load_mailboxes
from email_sorter.classifier import Decision
from email_sorter.sorter import RunResult
from email_sorter.store import Store
from email_sorter.web import admin
from email_sorter.web import senders as senders_web
from email_sorter.unsubscribe import UnsubscribeError
from email_sorter.web.editing import (EditError, rename_category_key, rename_folder_refs, save_sender_rules,
                                      write_secrets)

from test_editing import MAILBOX, PASSWORD, SHARED


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    write_secrets(tmp_path / "secrets.toml", "classifier", {"api_key": "k"})
    box_dir = tmp_path / "mailboxes" / "privat"
    box_dir.mkdir(parents=True)
    write_secrets(box_dir / "secrets.toml", "imap", {"user": "u", "password": "p"})
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
    from email_sorter.runtime import single_instance
    box = _box(setup)
    with single_instance(box.lock_path) as held:  # a run holds the lock
        assert held
        job = _wait(jobs.start(box, "run", "Lauf", lambda: RunResult(exit_code=0))["id"])
        assert job["status"] == "rejected"


# ---------------------------------------------------------------- manual + editing helpers

def test_move_mail_to_category_without_folder_needs_no_imap(setup, monkeypatch):
    box = _box(setup)
    monkeypatch.setattr(imap, "MailBox", None)  # would fail if IMAP were touched
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
    with monkeypatch.context() as m:  # jobs live in memory, so other tests may have left some
        m.setattr(admin.jobs, "recent", lambda box_id, n: [])
        assert "Letzte Jobs" not in client.get("/ui/m/privat/maintenance").text  # no empty card before the first
    html = client.get("/ui/m/privat/maintenance").text
    assert "Wartung · Privat" in html and "Verbindung prüfen" not in html  # checked under the settings now
    r = client.post("/ui/m/privat/maintenance/run", data={"csrf": _csrf(html), "limit": "20"}, follow_redirects=False)
    assert r.status_code == 303 and "/ui/m/privat/jobs/" in r.headers["location"]
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert calls == [(False, 20)]
    html = client.get(r.headers["location"]).text
    assert "Lauf (Probelauf)" in html and "Würde verschieben" in html
    assert "Lauf (Probelauf)" in client.get("/ui/m/privat/maintenance").text  # listed as recent job
    # "Ausführen" is the second submit button of each task; it posts live=1
    page = client.get("/ui/m/privat/maintenance").text
    assert page.count('>Probelauf</button>') == page.count('name="live" value="1"') == 9  # undo: on the run's page; the deletion rules have one
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
    r = client.post("/ui/m/privat/maintenance/rename_category", data={"csrf": token, "old": "werbung", "new": "inbox"})
    assert "„inbox“ ist reserviert" in r.text
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
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "move",
                                                       "category": "", "back": "period=all"})
    assert "Bitte eine Kategorie wählen." in r.text and len(moves) == 1  # "Correct" with nothing chosen
    # the star, and the way to the sender's other mails
    stars = []
    monkeypatch.setattr(admin, "set_star", lambda cfg, creds, ws, key, on: stars.append((key, on)))
    assert 'name="starred" value="1"' in html and 'aria-pressed="false"' in html
    assert 'href="/ui/m/privat/mails?q=Shop%40Example.de&amp;period=all">alle von diesem Absender</a>' in html
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "star",
                                                       "starred": "1", "back": "period=all&key=%3Cm1%40x%3E"})
    assert "Stern gesetzt." in r.text and stars == [("<m1@x>", True)] and "<h2>Unsicher</h2>" in r.text
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "star",
                                                       "starred": "", "back": "period=all&key=%3Cm1%40x%3E"})
    assert "Stern entfernt." in r.text and stars[-1] == ("<m1@x>", False)
    r = client.post("/ui/m/privat/mails/action", data={"csrf": token, "key": "<m1@x>", "action": "rule",
                                                       "match": "@example.de", "category": "werbung"})
    assert "Absender-Regel für @example.de gespeichert" in r.text and len(moves) == 1
    assert [(x.match, x.action) for x in _box(setup).cfg.sender_rules][-1] == ("@example.de", "werbung")


def test_accepting_a_mail_opens_the_next_one(client, setup, monkeypatch):
    monkeypatch.setattr(admin, "move_mail", lambda cfg, creds, ws, key, cat: "INBOX/Werbung")
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    for key, received in (("<m2@x>", "2026-09-19T10:00+02:00"), ("<m3@x>", "2026-09-18T10:00+02:00")):
        store.record(SimpleNamespace(key=key, received=received, sender="shop@example.de", subject=key,
                                     decision=Decision("werbung", 0.4, {"werbung": 0.4}, 0.1, 0.0001),
                                     folder=None, flag=False, expires=None, source="classifier"))
    store.close()
    html = client.get("/ui/m/privat/mails?uncertain=1&period=all&key=%3Cm2%40x%3E").text  # the middle one
    assert 'data-nav="prev" href="/ui/m/privat/mails?period=all&amp;uncertain=1&amp;page=1&amp;key=%3Cm1%40x%3E"' in html
    assert 'data-nav="next" href="/ui/m/privat/mails?period=all&amp;uncertain=1&amp;page=1&amp;key=%3Cm3%40x%3E"' in html
    then = re.search(r'name="then" value="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert then.endswith("key=%3Cm3%40x%3E")  # accepting goes on to the next one
    r = client.post("/ui/m/privat/mails/action", follow_redirects=False, data={
        "csrf": _csrf(html), "key": "<m2@x>", "action": "accept", "category": "werbung", "then": then,
        "back": "uncertain=1&period=all&page=1&key=%3Cm2%40x%3E"})
    assert r.headers["location"] == f"/ui/m/privat/mails?{then}"
    html = client.get(r.headers["location"]).text
    # what happened shows in the details of the mail that is open now, not at the top of the page
    notice = re.search(r'<div class="banner ok notice" role="status">([^<]+)</div>', html)
    assert notice and notice.group(1) == "„&lt;m2@x&gt;“: Verschoben nach INBOX/Werbung."
    assert html.count("Verschoben nach") == 1 and "<h2>&lt;m3@x&gt;</h2>" in html
    # the last one in the list has no next: accepting it goes back to the one before
    html = client.get("/ui/m/privat/mails?uncertain=1&period=all&key=%3Cm3%40x%3E").text
    assert 'data-nav="next"' not in html
    assert re.search(r'name="then" value="([^"]+)"', html).group(1).endswith("key=%3Cm2%40x%3E")


def test_add_mailbox(client, setup):
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Gmail", "imap_host": "imap.gmail.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "me@gmail.com", "imap_password": "app-pw", "template": "privat"}
    r = client.post("/ui/mailboxes/new", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/gmail/settings"
    raw = tomllib.loads((setup / "mailboxes" / "gmail" / "mailbox.toml").read_text(encoding="utf-8"))
    assert set(raw["imap"]) == {"host", "port", "source_folder"} and set(raw["categories"]) == {"finanzen", "werbung"}
    secrets = tomllib.loads((setup / "mailboxes" / "gmail" / "secrets.toml").read_text(encoding="utf-8"))
    assert secrets["imap"] == {"user": "me@gmail.com", "password": "app-pw"}
    html = client.get(r.headers["location"]).text
    assert "Postfach angelegt" in html and "gespeichert" in html and "app-pw" not in html
    r = client.post("/ui/mailboxes/new", data=form, follow_redirects=False)  # same name again: next free folder
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/gmail-2/settings"


def test_shared_settings(client, setup):
    html = client.get("/ui/settings").text
    assert "example/model-1" in html and 'name="api_key"' in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-2",
            "max_body_chars": "4000", "timeout_seconds": "30",
            "min_interval_seconds": "0,5"}
    r = client.post("/ui/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    cfg = _box(setup).cfg
    assert cfg.classifier_model == "example/model-2" and cfg.max_body_chars == 4000 and cfg.min_interval_seconds == 0.5
    r = client.post("/ui/settings", data={**form, "endpoint": "http://unsicher"})
    assert r.status_code == 422 and "https" in r.text



def test_log_out_everywhere(client, setup):
    other = TestClient(api.app)  # a second browser, logged in as well
    other.post("/login", data={"password": PASSWORD})
    assert other.get("/ui", follow_redirects=False).status_code == 200
    html = client.get("/ui/settings").text
    assert "Überall abmelden" in html
    r = client.post("/ui/sessions/end", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for browser in (client, other):  # both are logged out, the cookie of the other one no longer counts
        assert browser.get("/ui", follow_redirects=False).status_code == 303
    assert "sessions_ended" in tomllib.loads((setup / "secrets.toml").read_text(encoding="utf-8"))["ui"]
    other.post("/login", data={"password": PASSWORD})  # logging in again works at once
    assert other.get("/ui", follow_redirects=False).status_code == 200
    other.close()


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
            "source_folder": "INBOX", "imap_user": "me@gmail.com", "imap_password": "app-pw", "template": "privat"}
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
    assert 'value="_example_de" selected' in html and "Standard-Kategorien, Deutsch" in html  # UI language first
    assert 'value="_example_en"' in html and "Standard-Kategorien, English" in html
    form = {"csrf": _csrf(html), "name": "Privat", "id": "privat", "imap_host": "imap.example.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "u", "imap_password": "p", "template": "_example_de"}
    assert client.post("/ui/mailboxes/new", data=form, follow_redirects=False).status_code == 303
    box = _box(setup)
    assert box.name == "Privat" and "werbung" in box.cfg.categories and box.cfg.schedule_enabled
    assert box.cfg.sender_rules == ()


def test_undo_a_run_from_the_run_log(client, setup, monkeypatch):
    started = datetime(2026, 9, 30, 10, 0, 0)
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    store.track(started)
    store.record(SimpleNamespace(key="<m2@x>", received="2026-09-20T10:00+02:00", sender="a@b.de", subject="Rechnung",
                                 decision=Decision("werbung", 0.9, {}, 0.1, 0.0001), folder="INBOX/Werbung",
                                 flag=False, expires=None))
    store.record_run("run", None, started, started, RunResult(exit_code=0, live=True, classified=1, moved=1))
    store.close()
    calls = []

    def fake_undo(cfg, creds, ws, run, live, sort_again, keys=None):
        calls.append((run, live, sort_again, keys))
        return {"ok": True, "live": live, "exit_code": 0, "summary": "1 Mail(s) zurückverschoben"}

    monkeypatch.setattr(admin, "run_undo", fake_undo)
    link = "/ui/m/privat/undo?run=2026-09-30T10%3A00%3A00"
    assert f'href="{link}">Anzeigen</a>' in client.get("/ui/m/privat").text
    html = client.get("/ui/m/privat/maintenance").text  # the run can be picked there too
    assert '<option value="2026-09-30T10:00:00">' in html and 'action="/ui/m/privat/undo"' in html
    html = client.get(link).text                          # its page lists the mails, all chosen
    assert "Rechnung" in html and "a@b.de" in html and 'name="key" value="&lt;m2@x&gt;" checked' in html
    assert "1 lässt sich noch rückgängig machen" in html
    form = {"csrf": _csrf(html), "run": "2026-09-30T10:00:00", "chosen": "1", "sort_again": "1"}
    r = client.post("/ui/m/privat/maintenance/undo", data=form, follow_redirects=False)  # none ticked
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/undo?run=2026-09-30T10%3A00%3A00"
    assert "Bitte mindestens eine Mail auswählen" in client.get(r.headers["location"]).text
    r = client.post("/ui/m/privat/maintenance/undo", data={**form, "key": "<m2@x>"}, follow_redirects=False)
    _wait(r.headers["location"].rsplit("/", 1)[1])
    assert calls == [("2026-09-30T10:00:00", False, True, ["<m2@x>"])]
    page = client.get(r.headers["location"]).text
    assert "Lauf vom 30.09." in page and "1 Mail (Probelauf)" in page and "zurückverschoben" in page
    assert 'name="keys"' in page  # "run it for real" undoes the same chosen mails
    assert "Jetzt ausführen" in page  # the dry run can be run for real with the same choice


def test_senders_page_without_senders(client):
    html = client.get("/ui/m/privat/senders").text
    assert "Kein Absender mit Abmelde-Link" in html and 'name="category"' not in html  # nothing to filter yet
    html = client.get("/ui/m/privat/senders?category=werbung").text                   # a filter set: it stays
    assert "Kein Absender dieser Kategorie" in html and 'name="category"' in html


def test_senders_page_and_unsubscribe(client, setup, monkeypatch):
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    for key, sender, links in (("<n1@x>", "news@shop.example", (["https://shop.example/u"], True)),
                               ("<n2@x>", "club@verein.example", (["mailto:off@verein.example"], False))):
        store.record(SimpleNamespace(key=key, received=datetime.now().astimezone().isoformat(timespec="minutes"),
                                     sender=sender, subject="Angebot", decision=Decision("werbung", 0.9, {}, 0.1, 0.0),
                                     folder="INBOX/Werbung", flag=False, expires=None, unsubscribe=links))
    store.close()
    sent = []
    monkeypatch.setattr(senders_web, "one_click", lambda url: sent.append(url) or 200)

    html = client.get("/ui/m/privat/senders").text
    assert "Absender · Privat" in html and 'href="/ui/m/privat/senders" aria-current="page"' in html
    assert "news@shop.example" in html and "Von news@shop.example abmelden? Sortroom schickt die Abmeldung an shop.example." in html
    assert 'href="mailto:off@verein.example"' in html and "Als abgemeldet markieren" in html
    token = _csrf(html)
    r = client.post("/ui/m/privat/senders/action", data={"csrf": token, "address": "news@shop.example",
                                                          "action": "unsubscribe", "category": "werbung"})
    assert sent == ["https://shop.example/u"] and "Von news@shop.example abgemeldet" in r.text
    assert "Abgemeldet am" in r.text and str(r.url).endswith("/senders?category=werbung")
    r = client.post("/ui/m/privat/senders/action", data={"csrf": token, "address": "club@verein.example",
                                                          "action": "unsubscribe"})
    assert "bietet keine One-Click-Abmeldung" in r.text and len(sent) == 1
    client.post("/ui/m/privat/senders/action", data={"csrf": token, "address": "club@verein.example", "action": "mark"})
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    assert store.sender("club@verein.example")["method"] == "manual"
    assert store.sender("news@shop.example")["method"] == "one-click"
    store.close()
    r = client.post("/ui/m/privat/senders/action", data={"csrf": token, "address": "news@shop.example", "action": "reset"})
    assert "nicht mehr als abgemeldet markiert" in r.text


def test_no_unsubscribing_from_suspicious_senders(client, setup, monkeypatch):
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    links = (["https://bank-check.example/u", "mailto:off@bank-check.example"], True)
    store.record(SimpleNamespace(key="<p1@x>", received=datetime.now().astimezone().isoformat(timespec="minutes"),
                                 sender="alert@bank-check.example", subject="Konto gesperrt",
                                 decision=Decision("verdaechtig", 0.9, {}, 0.1, 0.0), folder="INBOX/Verdaechtig",
                                 flag=False, expires=None, unsubscribe=links))
    store.close()
    sent = []
    monkeypatch.setattr(senders_web, "one_click", lambda url: sent.append(url) or 200)

    html = client.get("/ui/m/privat/senders").text
    assert "alert@bank-check.example" in html and "Nicht abmelden" in html
    assert "1 Mail dieses Absenders wurde als verdächtig einsortiert" in html
    # no button, no link, no mail to the sender
    assert 'value="unsubscribe"' not in html and "bank-check.example/u" not in html and "mailto:" not in html
    r = client.post("/ui/m/privat/senders/action", data={"csrf": _csrf(html), "address": "alert@bank-check.example",
                                                          "action": "unsubscribe"})
    assert "hat verdächtige Mails geschickt" in r.text and sent == []
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    assert store.sender("alert@bank-check.example")["unsubscribed"] is None
    store.close()


def test_failed_unsubscribe_is_not_noted(client, setup, monkeypatch):
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    store.note_unsubscribe([("news@shop.example", ["https://shop.example/u"], True, None)])
    store.close()

    def refuse(url):
        raise UnsubscribeError("shop.example hat mit Fehler 500 geantwortet.")

    monkeypatch.setattr(senders_web, "one_click", refuse)
    token = _csrf(client.get("/ui/m/privat/maintenance").text)  # the sender has no mail in the log to list
    r = client.post("/ui/m/privat/senders/action", data={"csrf": token, "address": "news@shop.example",
                                                          "action": "unsubscribe"})
    assert "Abmelden von news@shop.example fehlgeschlagen" in r.text
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    assert store.sender("news@shop.example")["unsubscribed"] is None
    store.close()


def test_correction_rate_on_categories_and_overview(client, setup):
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    received = datetime.now().astimezone().isoformat(timespec="minutes")
    for i in range(4):
        store.record(SimpleNamespace(key=f"<w{i}@x>", received=received, sender="a@b.de", subject="Angebot",
                                     decision=Decision("werbung", 0.9, {}, 0.1, 0.0), folder="INBOX/Werbung",
                                     flag=False, expires=None, source="classifier"))
    store.set_correction("<w0@x>", "werbung", "finanzen", "ui")
    store.set_manual("<w0@x>", "finanzen", "INBOX/Finanzen")
    store.close()
    html = client.get("/ui/m/privat/categories?cat=werbung").text
    assert 'title="1 von 4 korrigiert">25 %</td>' in html
    assert "In den letzten 30 Tagen hat das Modell 4 Mails hier einsortiert; 1 davon (25 %)" in html
    assert "25 % in 30 Tagen von Hand korrigiert" in client.get("/ui/m/privat").text


def test_a_broken_mailbox_is_shown_and_the_rest_works(client, setup):
    broken = setup / "mailboxes" / "arbeit"
    broken.mkdir()
    (broken / "mailbox.toml").write_text("name = 'Arbeit'\n[imap\n", encoding="utf-8")
    html = client.get("/ui/m/privat").text
    assert "Die Einstellungen des Postfachs arbeit lassen sich nicht laden" in html
    assert "mailboxes/arbeit/mailbox.toml korrigieren" in html
    r = client.get("/ui/m/arbeit")
    assert r.status_code == 500 and "lassen sich nicht laden" in r.text
    html = client.get("/ui/settings").text  # a mailbox broken before doesn't block the global settings
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-3",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0"}
    assert client.post("/ui/settings", data=form, follow_redirects=False).status_code == 303
    assert _box(setup).cfg.classifier_model == "example/model-3"


def test_a_long_job_log_is_cut_with_a_note(setup):
    box = _box(setup)

    def chatty():
        for i in range(jobs.MAX_LOG_LINES + 50):
            logging.getLogger("email_sorter.test").info("line %d", i)
        return RunResult(exit_code=0)

    job = _wait(jobs.start(box, "run", "Lauf", chatty, needs_lock=False)["id"])
    assert len(job["log"]) == jobs.MAX_LOG_LINES + 1 and job["log"][-1] == "… (more lines in logs/sortroom.log)"


def test_suggestions_on_the_overview_and_after_a_correction(client, setup, monkeypatch):
    monkeypatch.setattr(admin, "move_mail", lambda cfg, creds, ws, key, cat: "INBOX/Werbung")
    html = client.get("/ui/m/privat/mails?period=all&key=%3Cm1%40x%3E").text
    r = client.post("/ui/m/privat/mails/action", follow_redirects=False, data={
        "csrf": _csrf(html), "key": "<m1@x>", "action": "move", "category": "werbung",
        "back": "period=all&key=%3Cm1%40x%3E"})
    assert r.headers["location"].endswith("&rule=werbung")       # corrected by hand: a rule is offered
    html = client.get(r.headers["location"]).text
    assert '<details class="more act" open>' in html and "Immer für diesen Absender?" in html
    assert '<option value="werbung" selected>nach Werbung</option>' in html

    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    for i in range(3):
        store.record(SimpleNamespace(key=f"<n{i}@x>", received=datetime.now().astimezone().isoformat(timespec="minutes"),
                                     sender="news@shop.example", subject="Angebot", source="classifier",
                                     decision=Decision("finanzen", 0.9, {}, 0.1, 0.0), folder="INBOX/Finanzen",
                                     flag=False, expires=None))
        store.set_correction(f"<n{i}@x>", "finanzen", "werbung", "ui")
    store.close()
    html = client.get("/ui/m/privat").text
    assert "Vorschläge" in html and "3 Mails von news@shop.example kamen von Hand nach „Werbung“" in html
    assert "vielleicht zu ungenau" not in html  # the suggested rule explains these corrections
    r = client.post("/ui/m/privat/suggestions", data={"csrf": _csrf(html), "kind": "rule", "action": "rule",
                                                      "subject": "news@shop.example", "target": "werbung"})
    assert "Absender-Regel für news@shop.example gespeichert" in r.text and "Vorschläge" not in r.text
    assert ("news@shop.example", "werbung") in [(x.match, x.action) for x in _box(setup).cfg.sender_rules]
