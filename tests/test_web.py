import hashlib
from datetime import date, datetime, timedelta, timezone, UTC
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
          source="classifier", expires=None, name=""):
    store.record(SimpleNamespace(
        key=key, received=(datetime.now().astimezone() - timedelta(days=2)).isoformat(timespec="minutes"),
        sender=sender, sender_name=name, subject=subject,
        decision=Decision(category, conf, {category: conf}, 0.9 if flagged else 0.1, 0.0001),
        folder=moved_to, flag=flagged, expires=expires, source=source))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    web._failed_logins.clear()
    boxes = {"privat": Mailbox("privat", "Privat", tmp_path / "privat", CFG),
             "gmail": Mailbox("gmail", "Gmail", tmp_path / "gmail", CFG)}
    store = Store(tmp_path / "privat" / "data" / "state.db")
    _mail(store, "<r1@x>", "finanzen", 0.97, "INBOX/Finanzen", "Ihre Rechnung Nr. 4711", flagged=True)
    _mail(store, "<r2@x>", "werbung", 1.0, "INBOX/Werbung", "20 % Rabatt nur heute", expires=date(2026, 9, 24))
    _mail(store, "<r3@x>", "finanzen", 0.45, None, "Ein Vertrag bucht im Juli 11,75 € ab", sender="dein@finanzguru.de",
          name="Max Mustermann")
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


def _clock(monkeypatch, start=1000.0):
    now = [start]
    monkeypatch.setattr(web, "_clock", lambda: now[0])
    return now


def test_failed_logins_lock_the_address_for_a_while(client, monkeypatch):
    now = _clock(monkeypatch)
    monkeypatch.setattr(web.time, "sleep", lambda s: pytest.fail("a login must never wait in a worker thread"))
    for _ in range(web.MAX_FAILED_PER_ADDRESS):
        assert client.post("/login", data={"password": "falsch"}).status_code == 401
        now[0] += 10
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)  # even the right one
    assert r.status_code == 429 and "Zu viele fehlgeschlagene Anmeldungen" in r.text and "15 Minuten" in r.text
    assert client.get("/ui", follow_redirects=False).status_code == 303
    now[0] = 1000.0 + web.FAILED_WINDOW + 1  # the first failure is old enough now
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and not web._failed_logins  # a login clears the address


def test_failed_logins_from_many_addresses_lock_everyone(client, monkeypatch):
    now = _clock(monkeypatch)
    web._failed_logins.update({f"10.0.0.{i}": [now[0]] for i in range(web.MAX_FAILED_TOTAL)})
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429
    now[0] += web.FAILED_WINDOW
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    assert "10.0.0.1" not in web._failed_logins  # old entries are dropped


def _csrf(html):
    return html.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_login_and_logout(client):
    _login(client)
    html = client.get("/ui").text
    assert client.post("/logout").status_code == 403            # without the form's token: another site
    assert client.post("/logout", data={"csrf": "falsch"}).status_code == 403
    assert client.get("/ui", follow_redirects=False).status_code == 200  # still logged in
    r = client.post("/logout", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/ui", follow_redirects=False).status_code == 303


def test_login_form_has_a_user_name_for_password_managers_and_can_show_the_password(client):
    html = client.get("/login").text
    # the one account, so a password manager offers to fill and save it; the script adds the show/hide button
    assert 'name="username" value="admin" autocomplete="username"' in html and 'autocomplete="current-password"' in html
    assert 'class="pw"' in html and 'data-show="Passwort anzeigen" data-hide="Passwort verbergen"' in html
    assert client.post("/login", data={"username": "admin", "password": "falsch"}).status_code == 401  # ignored


def test_api_documentation_needs_the_login(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    _login(client)
    assert "swagger-ui" in client.get("/docs").text and "redoc" in client.get("/redoc").text
    spec = client.get("/openapi.json").json()
    assert "/run" in spec["paths"] and "/docs" not in spec["paths"]


@pytest.mark.parametrize("target", ["//evil.example/x", "/\\evil.example", "/\\/evil.example", "/\t/evil.example",
                                    "/\n/evil.example", "https://evil.example", "evil.example"])
def test_login_does_not_redirect_elsewhere(client, target):
    r = client.post("/login", data={"password": PASSWORD, "next": target}, follow_redirects=False)
    assert r.headers["location"] == "/ui"


def test_login_returns_to_the_page_asked_for(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "/ui/m/privat/mails?q=a%20b&period=all"},
                    follow_redirects=False)
    assert r.headers["location"] == "/ui/m/privat/mails?q=a%20b&period=all"


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
    # privat: its failed backfill today shows in the status; the figures link to their lists
    assert "1 Lauf mit Fehlern heute" in html
    assert 'href="/ui/m/privat/mails?uncertain=1&amp;period=30d"' in html
    assert 'href="/ui/m/privat/mails?flagged=1&amp;period=7d"' in html
    assert 'href="/ui/m/privat/mails">' not in html  # no separate "Mails" button any more


def test_overview_page(client):
    html = _login(client).get("/ui/m/privat").text
    assert "<title>Übersicht · Privat · Sortroom</title>" in html          # the tab names the mailbox
    assert '<span class="eyebrow">Privat</span>\n    <h1>Übersicht</h1>' in html  # the page: above its title
    assert "Werbung" in html and "Finanzen" in html              # distribution bars
    # each bar opens its mails of the 7 days; left in the inbox: the inbox
    assert 'href="/ui/m/privat/mails?category=werbung&amp;period=7d"' in html
    assert 'href="/ui/m/privat/mails?folder=inbox&amp;period=7d"' in html
    # the figures: each a way to what is behind it, one period each
    assert 'class="card kpi link" href="/ui/m/privat/maintenance"' in html and "Mit Stern · 7 Tage" in html
    assert 'href="/ui/m/privat/mails?flagged=1&amp;period=7d"' in html and 'href="/ui/settings#model"' in html
    assert "abgelaufene Angebote" in html  # werbung tracks expiry dates
    assert "Backfill" in html and "Abgebrochen" in html          # run log with the failed backfill
    assert "1 Lauf mit Fehlern heute" in html
    # the runs: today in one line, the failed backfill worth a look with its message, the rest folded
    assert "Heute 2 Läufe · 3 Mails verschoben ·" in html and "1 mit Fehlern" in html
    assert '<ul class="notable-runs">' in html and "<code>socket error</code>" in html
    assert '<details class="more all-runs">' in html
    # the banner: what the error said, and what to do about it (a network error: the connection)
    assert "Backfill um " in html and 'href="/ui/m/privat/settings#connection">Verbindung prüfen</a>' in html
    assert "Ein Vertrag bucht im Juli" in html                    # "zur Prüfung"
    assert '<span class="from-name">Max Mustermann</span> <span class="from-addr">dein@finanzguru.de</span>' in html


def test_mails_page_filters_and_detail(client):
    c = _login(client)
    html = c.get("/ui/m/privat/mails?period=all").text
    assert "4 Einträge" in html and "Ihre Rechnung Nr. 4711" in html
    # the location shows only where it isn't the category's folder: here, the uncertain mail in the inbox
    assert html.count('class="where-note"') == 1 and '<span class="where-note">in Posteingang</span>' in html
    assert html.count('tabindex="-1"') == 4  # one tab stop per row: the subject, not the date
    assert '<span class="count">1</span>' in html and "Filter zurücksetzen" in html  # "all": not the default 7 days
    html = c.get("/ui/m/privat/mails").text
    assert "Filter zurücksetzen" not in html and '<span class="count">' not in html
    html = c.get("/ui/m/privat/mails?period=all&uncertain=1").text
    assert "1 Eintrag" in html and "Ein Vertrag bucht im Juli" in html and "Ihre Rechnung" not in html
    assert 'href="/ui/m/privat/mails">Filter zurücksetzen</a>' in html and '<span class="count">2</span>' in html
    html = c.get("/ui/m/privat/mails?period=all&q=lieferando").text
    assert "1 Eintrag" in html and "Regel" in html
    html = c.get("/ui/m/privat/mails?period=all&q=mustermann").text  # the display name is searched too
    assert "1 Eintrag" in html and '<span class="from-name">Max Mustermann</span>' in html
    assert '<span class="from-name">' not in c.get("/ui/m/privat/mails?period=all&q=lieferando").text  # none known
    html = c.get("/ui/m/privat/mails?period=all&folder=inbox").text
    assert "1 Eintrag" in html
    html = c.get("/ui/m/privat/mails?period=all&key=%3Cr2%40x%3E").text
    assert "Gültig bis 24.09.2026" in html and "Modell: Werbung · Sicherheit 1,00" in html
    assert '<option value="" selected disabled>Kategorie wählen …</option>' in html  # "Correct": nothing chosen
    assert "Ablaufdatum setzen" not in html  # a mail with a date: the field is open
    html = c.get("/ui/m/privat/mails?period=all&key=%3Cr3%40x%3E").text  # uncertain: why, and no date yet
    assert "Modell: Finanzen · Sicherheit 0,45" in html and "(mindestens 0,70 nötig)" in html
    assert ('<div class="muted detail-from"><span class="from-name">Max Mustermann</span> '
            '<span class="from-addr">dein@finanzguru.de</span> · <a href="/ui/m/privat/mails?q=dein%40finanzguru.de') in html
    assert "<summary>Ablaufdatum setzen</summary>" in html and "<summary>Mehr Details</summary>" in html
    # on a tablet the details lie over the list; the dimmed list behind them closes them
    assert '<a class="detail-backdrop" href="/ui/m/privat/mails?period=all&amp;page=1" tabindex="-1"' in html
    assert 'class="detail-backdrop"' not in c.get("/ui/m/privat/mails?period=all").text  # nothing open: none


def test_run_problems_and_their_banner():
    assert [web.run_problem(r) for r in (
        {"error": "HTTP 401: invalid api key"}, {"error": "the classification endpoint failed for 5 mails in a row"},
        {"error": "b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'"}, {"error": "[Errno 11001] getaddrinfo failed"},
        {"error": "folder INBOX/X not found"}, {"error": None, "failed": 2})] == ["model", "model", "imap", "imap", "", "mails"]
    assert web.run_error("b'[AUTHENTICATIONFAILED] Invalid credentials'") == "[AUTHENTICATIONFAILED] Invalid credentials"
    assert web.run_error("socket error") == "socket error" and web.run_error(None) == ""


def test_recovered_failure_is_only_noted(client, tmp_path):
    store = Store(tmp_path / "privat" / "data" / "state.db")
    later = datetime.now() + timedelta(minutes=1)  # the run log keeps seconds: after the failed one
    store.record_run("backfill", "since 2025-01-01", later, later, RunResult(exit_code=0, live=True, classified=1))
    store.close()
    html = _login(client).get("/ui/m/privat").text
    assert "Der nächste lief wieder durch." in html and "Verbindung prüfen" not in html


def test_search_treats_wildcards_literally(client):
    html = _login(client).get("/ui/m/privat/mails?period=all&q=%25").text
    assert "1 Eintrag" in html  # only "20 % Rabatt", not everything


def test_mails_sorted_and_filtered_by_received(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    now = datetime.now().astimezone()
    received = {  # offsets differ, so text order would be wrong
        "<utc>": (now - timedelta(hours=2)).astimezone(UTC).isoformat(timespec="minutes"),
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


def test_wide_fields_use_the_span_class():
    """A field two columns wide takes .span-2, which narrower screens undo; an inline grid-column: span 2
    would force a second column into the one-column layout and squeeze the fields beside it."""
    for path in (Path(web.__file__).parent / "templates").glob("*.html"):
        assert "grid-column:span" not in path.read_text(encoding="utf-8").replace(" ", ""), path.name


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
    utc = datetime(year, 9, 24, 10, 0, tzinfo=UTC)
    assert i18n.dt(utc.isoformat()) == utc.astimezone().strftime("%d.%m. %H:%M")  # sender's offset -> local
    assert i18n.usd(0.00009, 5) == "0,00009\u00a0$"


def test_small_costs_keep_two_significant_digits():
    usd = i18n.usd
    token = i18n.set_language("en")
    try:
        assert usd(0) == "$0.00" and usd(None) == "$0.00" and usd(0, 4) == "$0.0000"
        assert usd(12.345) == "$12.35" and usd(0.5) == "$0.50" and usd(1234.5) == "$1,234.50"  # a cent or more
        assert usd(0.0123) == "$0.012"
        assert usd(0.00042) == "$0.00042" and usd(0.0004) == "$0.0004"  # no trailing zero beyond the minimum
        assert usd(0.00002, 4) == "$0.00002" and usd(0.000021) == "$0.000021" and usd(0.00009, 5) == "$0.00009"
        assert usd(0.0000004) == "<$0.000001"                           # below what the log keeps
    finally:
        i18n.reset_language(token)
    # German: decimal comma and the sign after the amount, as for percentages
    assert usd(0.00042) == "0,00042\u00a0$" and usd(1234.5) == "1 234,50\u00a0$" and usd(0) == "0,00\u00a0$"
    assert usd(0.0000004) == "<0,000001\u00a0$"


def test_all_mailboxes_subtitle_says_what_is_going_on(client, monkeypatch):
    c = _login(client)
    html = c.get("/ui").text  # privat's backfill failed today; the schedule is off in tests
    assert ('<a class="sub-warn" href="#box-privat">1 Postfach mit Fehlern heute</a> · Zeitplan in diesem Prozess '
            'abgeschaltet') in html
    status = {"enabled": True, "active": True, "running": False, "minutes": 10, "next": datetime(2026, 9, 27, 17, 45)}
    monkeypatch.setattr(api.app.state.scheduler, "status", lambda box: dict(status))
    assert "Nächster Lauf 17:45" in c.get("/ui").text
    monkeypatch.setattr(api.app.state, "is_busy", lambda box: box.id == "privat")
    html = c.get("/ui").text
    assert "Läuft gerade: Privat" in html and "Nächster Lauf" not in html.split('class="sub"')[1].split("</div>")[0]


def test_all_mailboxes_sums_in_the_subtitle_review_button_quiet_schedule_and_login(client, monkeypatch):
    c = _login(client)
    html = c.get("/ui").text
    # the sums: one line below the subtitle (two mailboxes), not cards of their own
    assert 'class="grid totals"' not in html
    assert '<div class="sub totals-line">' in html and "heute einsortiert ·" in html and "diesen Monat</div>" in html
    # the review is what the card is for: its count stands out and the button starts it (privat has an uncertain mail)
    assert 'class="stat needs-review" href="/ui/m/privat/mails?uncertain=1&amp;period=30d"' in html
    assert ('class="btn primary" href="/ui/m/privat/mails?uncertain=1&amp;period=30d&amp;key=%3Cr3%40x%3E">'
            'Prüfen (1)</a>') in html
    assert 'class="stat" href="/ui/m/privat"><span>Kosten diesen Monat' in html  # the cost: on to the Overview
    # the server only where the name doesn't say the account; no login saved: said, with the way
    assert CFG.imap_host in html
    assert "Noch kein Login gespeichert" in html and 'href="/ui/m/gmail/settings#connection">Login eintragen</a>' in html
    # the usual schedule isn't repeated on every card: only what is not usual
    usual = {"enabled": True, "active": True, "running": False, "minutes": 10, "next": datetime(2026, 9, 27, 17, 45)}
    monkeypatch.setattr(api.app.state.scheduler, "status", lambda box: dict(usual))
    html = c.get("/ui").text
    assert "Nächster Lauf 17:45" in html.split('class="sub"')[1].split("</div>")[0]  # in the subtitle
    assert "alle 10 Min" not in html
    monkeypatch.setattr(api.app.state.scheduler, "status",
                        lambda box: {**usual, "minutes": 30 if box.id == "gmail" else 10})
    assert html.count("alle 10 Min") == 0 and c.get("/ui").text.count("alle 10 Min") == 1  # intervals differ: said


def test_all_mailboxes_an_error_is_shown_with_its_way_out_and_comes_first(client, tmp_path):
    c = _login(client)
    html = c.get("/ui").text
    # privat's failed backfill (a network error) is still open: its card first, with what it said and the way out
    assert html.index('id="box-privat"') < html.index('id="box-gmail"')
    assert 'class="card boxcard problem" id="box-privat"' in html and "<code>socket error</code>" in html
    assert 'href="/ui/m/privat/settings#connection">Verbindung prüfen</a>' in html
    # once a later run of the same kind went through, the card only notes it, and the order is as it was
    store = Store(tmp_path / "privat" / "data" / "state.db")
    later = datetime.now() + timedelta(minutes=1)
    store.record_run("backfill", "since 2025-01-01", later, later, RunResult(exit_code=0, live=True, classified=1))
    store.close()
    html = c.get("/ui").text
    assert "Heute ein Fehler, aber der nächste Lauf lief wieder durch." in html and "Verbindung prüfen" not in html
    assert 'class="card boxcard problem"' not in html and "Postfach mit Fehlern heute" not in html


def test_sidebar_lists_the_mailboxes(client):
    html = _login(client).get("/ui/m/privat/mails?period=all").text
    assert 'class="switcher"' not in html                      # no dropdown any more
    assert 'href="/ui/m/gmail/mails"' in html                   # switching mailboxes keeps the page
    assert 'href="/ui/m/privat/mails?uncertain=1&amp;period=30d"' in html and "1 Mail zur Prüfung" in html  # badge
    assert 'title="Noch kein Lauf"' in html                     # status dot explained
    logout = html.split('<form method="post" action="/logout">', 1)[1].split("</form>", 1)[0]
    assert "<svg" in logout and logout.rstrip().endswith("Abmelden</button>")  # a nav entry with an icon
