from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from email_sorter import digest, mail, notify, scheduler
from email_sorter.classifier import Decision
from email_sorter.config import load_mailboxes
from email_sorter.sorter import RunResult
from email_sorter.store import Store
from email_sorter.web.editing import write_secrets
from test_editing import _csrf, client, setup  # noqa: F401  (fixtures)

MAIL = """
[mail]
host = "smtp.example.com"
port = 587
security = "starttls"
user = "me@example.com"
sender = "Sortroom <me@example.com>"
recipient = "me@example.com"
ui_url = "http://nas:8765/"
notify_failures = true
digest = true
digest_time = "07:00"
"""
NOW = datetime(2026, 10, 3, 8, 0)


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append((self.calls, msg))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP.sent


@pytest.fixture
def configured(setup):  # noqa: F811
    path = setup / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + MAIL, encoding="utf-8")
    write_secrets(setup / "secrets.toml", "smtp", {"password": "smtp-pw"})
    return path


def _box(setup):  # noqa: F811
    return load_mailboxes(setup, setup / "config.toml")["privat"]


def test_settings_and_sending(configured, smtp):
    s = mail.mail_settings(configured)
    assert s.ready and s.port == 587 and s.link("/ui/m/x") == "http://nas:8765/ui/m/x"
    assert mail.smtp_password(configured) == "smtp-pw"
    mail.send(s, "smtp-pw", "Betreff", "Text äöü\n")
    calls, msg = smtp[0]
    assert calls == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "me@example.com", "smtp-pw")]
    assert msg["To"] == "me@example.com" and msg["Auto-Submitted"] == "auto-generated"
    assert msg.get_content() == "Text äöü\n"
    assert not mail.mail_settings(configured.with_name("missing.toml")).ready      # nothing set up: nothing sent
    with pytest.raises(mail.MailError, match="not set up"):
        mail.send(mail.MailSettings(), "", "x", "y")


def test_one_mail_per_incident_and_one_when_it_works_again(configured, smtp):
    box = _box(configured.parent)
    notify.report(configured, box, "run", True, "IMAP login failed", now=NOW)
    notify.report(configured, box, "run", True, "IMAP login failed")         # still failing: no second mail
    notify.report(configured, box, "reconcile", False)                       # another task, fine: nothing
    assert len(smtp) == 1
    msg = smtp[0][1]
    assert "fehlgeschlagen" in msg["Subject"] and "IMAP login failed" in msg.get_content()
    assert "http://nas:8765/ui/m/privat" in msg.get_content()
    notify.report(configured, box, "run", False)
    assert len(smtp) == 2 and "geht wieder" in smtp[1][1]["Subject"]
    notify.report(configured, box, "run", False)                             # fine before: nothing
    assert len(smtp) == 2


def test_no_notification_when_switched_off_or_sending_fails(configured, smtp, monkeypatch):
    box = _box(configured.parent)
    configured.write_text(configured.read_text(encoding="utf-8").replace("notify_failures = true",
                                                                         "notify_failures = false"), encoding="utf-8")
    notify.report(configured, box, "run", True, "x")
    assert smtp == []
    configured.write_text(configured.read_text(encoding="utf-8").replace("notify_failures = false",
                                                                         "notify_failures = true"), encoding="utf-8")
    monkeypatch.setattr(notify, "send_configured", lambda *a: False)         # the server is down
    notify.report(configured, box, "run", True, "x")
    store = Store(box.workspace / "data" / "state.db")
    assert store.meta("alert:run") is None                                    # so it is tried again next time
    store.close()


def _record(store, key, received, **kw):
    store.record(SimpleNamespace(key=key, received=received.isoformat(timespec="minutes"), sender="s@shop.example",
                                 subject=key, decision=Decision(kw.get("category", "werbung"), kw.get("conf", 0.9),
                                                                {}, 0.0, 0.0),
                                 folder=None if kw.get("conf", 0.9) < 0.5 else "INBOX/Werbung",
                                 flag=kw.get("flag", False), expires=kw.get("expires"), source="classifier"))


def test_the_daily_summary(configured, smtp):
    box = _box(configured.parent)
    store = Store(box.workspace / "data" / "state.db")
    now = datetime.now()
    _record(store, "unsure", now - timedelta(hours=3), conf=0.3)
    _record(store, "bill", now - timedelta(hours=2), flag=True)
    _record(store, "offer", now - timedelta(days=3), expires=date.today() + timedelta(days=1))
    store.record_run("run", None, now - timedelta(hours=1), now, RunResult(exit_code=2, live=True, error="login failed"))
    store.close()
    subject, body = digest.compose(configured, {"privat": box})
    assert subject == "Sortroom: 1 zu prüfen, 1 mit Stern"
    assert "Zu prüfen: 1 – http://nas:8765/ui/m/privat/mails?uncertain=1&period=30d" in body
    assert "unsure · s@shop.example" in body and "bill" in body and "offer" in body and "login failed" in body

    sent_dir = configured.parent / "mailboxes"
    morning = datetime.combine(date.today(), datetime.min.time()).replace(hour=6, minute=59)
    assert not digest.due(configured, sent_dir, morning)                      # before 07:00
    assert digest.due(configured, sent_dir, morning + timedelta(minutes=1))
    assert digest.send_digest(configured, lambda: {"privat": box}, sent_dir, morning + timedelta(minutes=1))
    assert len(smtp) == 1 and not digest.due(configured, sent_dir, morning + timedelta(hours=2))  # once a day


def test_no_summary_on_a_quiet_day(configured, smtp):
    box = _box(configured.parent)
    assert digest.compose(configured, {"privat": box}) is None
    assert digest.send_digest(configured, lambda: {"privat": box}, configured.parent / "mailboxes")
    assert smtp == []


def test_the_schedule_reports_outcomes_and_sends_the_summary_once():
    box = SimpleNamespace(id="privat", cfg=SimpleNamespace(schedule_enabled=True, schedule_minutes=10,
                                                           reconcile_enabled=False, reconcile_hours=24, delete_rules=()))
    reports, sent, due = [], [], {"now": True}
    s = scheduler.Scheduler(lambda: {"privat": box}, run_box=lambda b: (True, "login failed"),
                            report=lambda b, task, failed, why: reports.append((b.id, task, failed, why)),
                            digest_due=lambda now: due["now"],
                            send_digest=lambda: sent.append(1) or due.update(now=False))  # sent: not due
    started = s.tick(NOW)
    assert "digest" in started
    s.tick(NOW + scheduler.FIRST_RUN_DELAY)
    for _ in range(200):
        if not s.running and not s.digesting:
            break
        __import__("time").sleep(0.01)
    assert reports == [("privat", "run", True, "login failed")] and sent == [1]


def test_mail_settings_in_global_settings(client, setup, smtp, monkeypatch):  # noqa: F811
    html = client.get("/ui/settings").text
    assert 'name="smtp_host"' in html and "Testmail" in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "de",
            "smtp_host": "smtp.example.com", "smtp_port": "465", "smtp_security": "ssl", "smtp_user": "me@example.com",
            "smtp_password": "geheim", "mail_sender": "me@example.com", "mail_recipient": "me@example.com",
            "ui_url": "http://nas:8765", "notify_failures": "1", "digest": "1", "digest_time": "06:30"}
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    r = client.post("/ui/settings", data={**form, "then": "testmail"})
    assert "Testmail an me@example.com geschickt" in r.text
    assert r.text.index('id="mail"') < r.text.index("Testmail an me@example.com geschickt")  # in the Mail card
    assert "an me@example.com geschickt" not in client.get("/ui/settings").text               # shown once
    s = mail.mail_settings(setup / "config.toml")
    assert (s.host, s.port, s.security, s.digest_time, s.notify_failures) == ("smtp.example.com", 465, "ssl", "06:30", True)
    assert mail.smtp_password(setup / "config.toml") == "geheim" and "geheim" not in r.text
    assert smtp[0][0][-1] == ("login", "me@example.com", "geheim")
    for bad, message in (({"mail_recipient": ""}, "SMTP-Server, Absender und Empfänger"),
                         ({"mail_sender": "kein-mail"}, "E-Mail-Adresse"), ({"digest_time": "25:00"}, "Uhrzeit"),
                         ({"ui_url": "nas:8765"}, "http://")):
        r = client.post("/ui/settings", data={**form, "csrf": _csrf(html), **bad})
        assert r.status_code == 422 and message in r.text and "geheim" not in r.text
