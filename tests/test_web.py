import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from email_sorter import __version__, api, i18n, web
from email_sorter.web import queries
from email_sorter.config import Mailbox
from email_sorter.classifier import Decision
from email_sorter.sorter import RunResult
from email_sorter.store import Store
from support import example_config

CFG = example_config()
PASSWORD = "richtig-geheim"


def _mail(store, key, category, conf, moved_to, subject, sender="shop@example.de", flagged=False,
          source="classifier", expires=None):
    store.record(SimpleNamespace(
        key=key, received=(datetime.now().astimezone() - timedelta(days=2)).isoformat(timespec="minutes"),
        sender=sender, subject=subject,
        decision=Decision(category, conf, {category: conf}, 0.9 if flagged else 0.1, 0.0001),
        folder=moved_to, flag=flagged, expires=expires, source=source))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)  # no login throttling in tests
    web._failed_logins.clear()
    boxes = {"privat": Mailbox("privat", "Privat", tmp_path / "privat", CFG),
             "gmail": Mailbox("gmail", "Gmail", tmp_path / "gmail", CFG)}
    store = Store(tmp_path / "privat" / "data" / "state.db")
    _mail(store, "<r1@x>", "finanzen", 0.97, "INBOX/Finanzen", "Ihre Rechnung Nr. 4711", flagged=True)
    _mail(store, "<r2@x>", "werbung", 1.0, "INBOX/Werbung", "20 % Rabatt nur heute", expires=date(2026, 9, 24))
    _mail(store, "<r3@x>", "finanzen", 0.45, None, "Ein Vertrag bucht im Juli 11,75 € ab", sender="dein@finanzguru.de")
    _mail(store, "<r4@x>", "werbung", 1.0, "INBOX/Werbung", "Lieferando Gutschein", source="rule")
    now = datetime.now()
    store.record_run("run", None, now, now,  # "now": stays "today" even right after midnight
                     RunResult(exit_code=0, live=True, classified=4, moved=3, cost_usd=0.0004,
                               categories={"werbung": 2, "finanzen": 2}))
    store.record_run("backfill", "since 2025-01-01", now, now,
                     RunResult(exit_code=1, live=True, classified=10, failed=1, error="socket error"))
    store.close()
    monkeypatch.setattr(api, "load_mailboxes", lambda base, path: boxes)
    with TestClient(api.app) as c:
        yield c


def _login(c):
    r = c.post("/login", data={"password": PASSWORD, "next": "/ui"}, follow_redirects=False)
    assert r.status_code == 303
    return c


# ---------------------------------------------------------------- auth

def test_pages_redirect_to_login(client):
    r = client.get("/ui/m/privat/mails?period=all", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/ui/m/privat/mails%3Fperiod%3Dall"


def test_wrong_password(client):
    r = client.post("/login", data={"password": "falsch", "next": "/ui"}, follow_redirects=False)
    assert r.status_code == 401 and "Falsches Passwort" in r.text
    assert client.get("/ui", follow_redirects=False).status_code == 303


def test_failed_logins_are_throttled(client, monkeypatch):
    waits = []
    monkeypatch.setattr(web.time, "sleep", waits.append)
    for _ in range(3):
        client.post("/login", data={"password": "falsch"})
    assert waits == [0, 1, 2]


def test_login_and_logout(client):
    _login(client)
    assert client.get("/ui").status_code == 200
    client.post("/logout")
    assert client.get("/ui", follow_redirects=False).status_code == 303


def test_login_does_not_redirect_elsewhere(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/ui"


def test_locked_without_admin_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    r = client.get("/login")
    assert "ADMIN_PASSWORD" in r.text
    r = client.post("/login", data={"password": ""}, follow_redirects=False)
    assert r.status_code == 401 and client.get("/ui", follow_redirects=False).status_code == 303


def test_api_token_endpoints_unaffected_by_session(client):
    _login(client)
    assert client.post("/run").status_code in (401, 500)  # still needs the bearer token


# ---------------------------------------------------------------- pages

def test_all_mailboxes_page(client):
    html = _login(client).get("/ui").text
    assert "Alle Postfächer" in html and "Privat" in html and "Gmail" in html
    assert "Noch kein Lauf" in html  # gmail has no log yet
    assert 'href="/ui/settings" >' in html and "Globale Einstellungen</a>" in html  # sidebar footer, not current


def test_overview_page(client):
    html = _login(client).get("/ui/m/privat").text
    assert "Übersicht · Privat" in html
    assert "Werbung" in html and "Finanzen" in html              # distribution bars
    assert "Backfill" in html and "Abgebrochen" in html          # run log with the failed backfill
    assert "1 Lauf mit Fehlern heute" in html
    assert "Ein Vertrag bucht im Juli" in html                    # "zur Prüfung"


def test_mails_page_filters_and_detail(client):
    c = _login(client)
    html = c.get("/ui/m/privat/mails?period=all").text
    assert "4 Einträge" in html and "Ihre Rechnung Nr. 4711" in html
    html = c.get("/ui/m/privat/mails?period=all&uncertain=1").text
    assert "1 Eintrag" in html and "Ein Vertrag bucht im Juli" in html and "Ihre Rechnung" not in html
    html = c.get("/ui/m/privat/mails?period=all&q=lieferando").text
    assert "1 Eintrag" in html and "Regel" in html
    html = c.get("/ui/m/privat/mails?period=all&folder=inbox").text
    assert "1 Eintrag" in html
    html = c.get("/ui/m/privat/mails?period=all&key=%3Cr2%40x%3E").text
    assert "Gültig bis" in html and "24.09.2026" in html and "Entschieden von" in html


def test_search_treats_wildcards_literally(client):
    html = _login(client).get("/ui/m/privat/mails?period=all&q=%25").text
    assert "1 Eintrag" in html  # only "20 % Rabatt", not everything


def test_mails_sorted_and_filtered_by_received(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    now = datetime.now().astimezone()
    received = {  # offsets differ, so text order would be wrong
        "<utc>": (now - timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="minutes"),
        "<la>": (now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=-7))).isoformat(timespec="minutes"),
        "<local>": (now - timedelta(hours=3)).isoformat(timespec="minutes"),
        "<backfilled>": (now - timedelta(days=40)).isoformat(timespec="minutes"),  # processed today, received long ago
    }
    for key, value in received.items():
        store.record(SimpleNamespace(key=key, received=value, sender="s", subject=key,
                                     decision=Decision("werbung", 0.5, {"werbung": 0.5}, 0.1, 0.0001),
                                     folder=None, flag=False, expires=None, source="classifier"))
    store.close()
    db = queries.connect(tmp_path)
    try:
        rows, total = queries.mails(db, queries.MailFilter(period="30d"), 0.7)
        assert [r["message_key"] for r in rows] == ["<la>", "<utc>", "<local>"] and total == 3
        rows, _ = queries.mails(db, queries.MailFilter(period="all"), 0.7)
        assert rows[-1]["message_key"] == "<backfilled>"
        assert queries.stats(db, 0.7).uncertain == 3
        assert [m["message_key"] for m in queries.uncertain_mails(db, 0.7)][0] == "<la>"
        assert len(queries.uncertain_mails(db, 0.7)) == 4                  # the overview limits it like the count
        assert len(queries.uncertain_mails(db, 0.7, days=30)) == 3
        assert queries.distribution(db, 7) == [("", 3)]                     # the backfill doesn't swamp the week
        assert queries.category_counts(db) == {"werbung": 3}
    finally:
        db.close()


def test_empty_mailbox_pages(client):
    c = _login(client)
    assert "Noch kein Lauf protokolliert" in c.get("/ui/m/gmail").text
    assert "Keine Mails für diese Filter" in c.get("/ui/m/gmail/mails").text


def test_unknown_mailbox_404(client):
    assert _login(client).get("/ui/m/nope").status_code == 404


def test_static_urls_change_with_content(client):
    html = client.get("/login").text
    css = (Path(web.__file__).parent / "static" / "app.css").read_bytes()
    url = f"/ui/static/app.css?v={hashlib.sha256(css).hexdigest()[:10]}"
    assert f'href="{url}"' in html and client.get(url).status_code == 200


def test_static_css_and_icons_served(client):
    assert client.get("/ui/static/app.css").status_code == 200
    assert client.get("/ui/static/icon.svg").status_code == 200
    assert client.get("/ui/static/icon-small.svg").status_code == 200
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert 'rel="icon"' in client.get("/login").text


def test_source_link_for_agpl(client):
    assert "github.com/mhoenes/sortroom" in client.get("/login").text
    html = _login(client).get("/ui").text
    assert f"v{__version__} · Quellcode · AGPL-3.0" in html  # the version lives in the footer only
    assert f"v{__version__}<" not in html                    # not under the brand any more


def test_formatters():  # German (the tests' default language); English in test_i18n.py
    assert i18n.num(12345) == "12 345"
    assert i18n.conf(0.456) == "0,46"
    year = date.today().year
    assert i18n.dt(f"{year}-09-24T21:58:00") == "24.09. 21:58"
    assert i18n.dt("2020-01-14T16:32:00") == "14.01.2020"            # earlier year: date only
    assert i18n.dt("2020-01-14T16:32:00", True) == "14.01.2020 16:32"
    utc = datetime(year, 9, 24, 10, 0, tzinfo=timezone.utc)
    assert i18n.dt(utc.isoformat()) == utc.astimezone().strftime("%d.%m. %H:%M")  # sender's offset -> local
    assert web.usd(0.00009, 5) == "$0.00009"
