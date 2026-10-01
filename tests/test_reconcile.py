from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from email_sorter import imap, reconcile
from email_sorter.config import Credentials
from email_sorter.classifier import Decision
from email_sorter.store import Store
from email_sorter.web import queries
from support import HeaderFetch, example_config, raw_headers

CFG = example_config()
CREDS = Credentials("u", "p", "k")


class FakeFolders:
    def __init__(self, mails, flags=None):
        self.mails, self.flags, self.current = mails, flags or {}, None  # server folder -> [message ids]

    def list(self, pattern=""):
        return [SimpleNamespace(name=n, delim=".", flags=self.flags.get(n, ())) for n in self.mails]

    def set(self, name):
        self.current = name


class FakeMailBox(HeaderFetch):
    def __init__(self, mails, flags=None):
        self.folder = FakeFolders(mails, flags)

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raw_mails(self):
        return [raw_headers(mid) for mid in self.folder.mails[self.folder.current]]


def _record(store, key, moved_to, confidence=0.9):
    store.record(SimpleNamespace(key=key, received="2026-09-20T10:00+02:00", sender="s", subject=key,
                                 decision=Decision("werbung", confidence, {}, 0.0, 0.0), folder=moved_to,
                                 flag=False, expires=None, source="classifier"))


def _setup(tmp_path, monkeypatch, mails, flags=None):
    monkeypatch.setattr(imap, "MailBox", lambda *a, **kw: FakeMailBox(mails, flags))
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "<kept@x>", "INBOX/Werbung")
    _record(store, "<deleted@x>", "INBOX/Werbung")
    _record(store, "<trash@x>", None, confidence=0.3)       # uncertain, then deleted to the trash
    _record(store, "<filed@x>", None, confidence=0.3)       # uncertain, then filed by hand
    _record(store, "<inbox@x>", None, confidence=0.3)       # uncertain, still in the inbox
    store.close()


MAILS = {"INBOX": ["<inbox@x>"], "INBOX.Werbung": ["<kept@x>"], "INBOX.Finanzen": ["<filed@x>"],
         "INBOX.Trash": ["<trash@x>"], "INBOX.Gesendet": ["<deleted@x>"]}


def test_reconcile_marks_deleted_and_filed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, MAILS, flags={"INBOX.Trash": ("\\Trash",)})
    result = reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)
    assert (result["gone"], result["back"], result["filed"]) == (2, 0, 1)
    store = Store(tmp_path / "data" / "state.db")
    rows = {k: (m, g) for k, m, g in store.locations()}
    store.close()
    assert rows["<deleted@x>"] == ("INBOX/Werbung", 1)   # only in "Gesendet", which is skipped
    assert rows["<trash@x>"] == (None, 1)
    assert rows["<filed@x>"] == ("INBOX/Finanzen", 0)
    assert rows["<kept@x>"] == ("INBOX/Werbung", 0) and rows["<inbox@x>"] == (None, 0)

    db = queries.connect(tmp_path)
    try:
        assert [m["message_key"] for m in queries.uncertain_mails(db, 0.7)] == ["<inbox@x>"]
        assert queries.stats(db, 0.7, now=datetime(2026, 9, 25)).uncertain == 1
        assert queries.mails(db, queries.MailFilter(period="all"), 0.7)[1] == 3           # deleted ones hidden
        assert queries.mails(db, queries.MailFilter(period="all", show_gone=True), 0.7)[1] == 5
    finally:
        db.close()


def test_reconcile_dry_run_and_reappearing_mail(tmp_path, monkeypatch):
    from email_sorter.store import last_reconcile
    _setup(tmp_path, monkeypatch, MAILS)
    assert reconcile.run_reconcile(CFG, CREDS, tmp_path, live=False)["gone"] == 2  # "Trash" skipped by name, too
    assert last_reconcile(tmp_path) is None  # a dry run doesn't count for the schedule
    store = Store(tmp_path / "data" / "state.db")
    assert not any(g for _, _, g in store.locations())
    store.set_gone(["<kept@x>"], True)
    store.close()
    assert reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)["back"] == 1
    assert last_reconcile(tmp_path) is not None


def test_gmail_all_mail_counts_as_kept_but_not_as_place(tmp_path, monkeypatch):
    mails = {"INBOX": ["<inbox@x>"], "Werbung": ["<kept@x>"],
             "[Gmail]/Alle Nachrichten": ["<inbox@x>", "<kept@x>", "<filed@x>", "<deleted@x>"],
             "[Gmail]/Papierkorb": ["<trash@x>"]}
    flags = {"[Gmail]/Alle Nachrichten": ("\\All",), "[Gmail]/Papierkorb": ("\\Trash",)}
    _setup(tmp_path, monkeypatch, mails, flags)
    result = reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)
    assert (result["gone"], result["filed"]) == (1, 0)   # only the trashed one; archived ones are kept
    store = Store(tmp_path / "data" / "state.db")
    rows = {k: (m, g) for k, m, g in store.locations()}
    store.close()
    assert rows["<filed@x>"] == (None, 0) and rows["<trash@x>"] == (None, 1)


def test_old_source_name_is_renamed_when_the_log_is_opened(tmp_path):
    import sqlite3
    Store(tmp_path / "state.db").close()
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("INSERT INTO processed (message_key, processed_at, category, confidence, needs_action, flagged, "
               "cost_usd, source) VALUES ('<old@x>', '2026-01-01T00:00:00', 'werbung', 0.9, 0, 0, 0, 'jev')")
    db.commit()
    db.close()
    Store(tmp_path / "state.db").close()
    db = sqlite3.connect(tmp_path / "state.db")
    assert db.execute("SELECT source FROM processed").fetchall() == [("classifier",)]
    db.close()


def test_only_a_few_header_lines_are_fetched_in_batches():
    from email_sorter.mailtext import message_key, unsubscribe_links

    class Folder(HeaderFetch):
        def raw_mails(self):
            return [raw_headers(f"<m{i}@x>", "News <news@shop.example>", list_unsubscribe="<https://shop/u>")
                    for i in range(imap.UID_CHUNK + 5)]

    mb = Folder()
    heads = list(imap.header_fields(mb))
    assert [message_key(h) for h in heads[:2]] == ["<m0@x>", "<m1@x>"] and len(heads) == imap.UID_CHUNK + 5
    assert heads[0].from_ == "news@shop.example" and unsubscribe_links(heads[0]) == (["https://shop/u"], False)
    assert heads[0].date.year == 2026
    assert len(mb.fetch_commands) == 2  # UID_CHUNK per FETCH
    assert mb.fetch_commands[0] == ("(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE "
                                    "LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])")  # PEEK: nothing becomes read
