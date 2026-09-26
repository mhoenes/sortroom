from pathlib import Path
from types import SimpleNamespace

from email_sorter import reconcile
from email_sorter.config import Credentials, load_config
from email_sorter.jev import Decision
from email_sorter.store import Store
from email_sorter.web import queries

CFG = load_config(Path(__file__).resolve().parent.parent / "config.toml")
CREDS = Credentials("u", "p", "k")


class FakeFolders:
    def __init__(self, mails, flags=None):
        self.mails, self.flags, self.current = mails, flags or {}, None  # server folder -> [message ids]

    def list(self, pattern=""):
        return [SimpleNamespace(name=n, delim=".", flags=self.flags.get(n, ())) for n in self.mails]

    def set(self, name):
        self.current = name


class FakeMailBox:
    def __init__(self, mails, flags=None):
        self.folder = FakeFolders(mails, flags)

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, *a, **kw):
        for mid in self.folder.mails[self.folder.current]:
            yield SimpleNamespace(headers={"message-id": (mid,)}, from_="s", subject="x", date_str="")


def _record(store, key, moved_to, confidence=0.9):
    store.record(SimpleNamespace(key=key, received="2026-09-20T10:00+02:00", sender="s", subject=key,
                                 decision=Decision("werbung", confidence, {}, 0.0, 0.0), folder=moved_to,
                                 flag=False, expires=None, source="jev"))


def _setup(tmp_path, monkeypatch, mails, flags=None):
    monkeypatch.setattr(reconcile, "MailBox", lambda *a, **kw: FakeMailBox(mails, flags))
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
        assert queries.stats(db, 0.7).uncertain == 1
        assert queries.mails(db, queries.MailFilter(period="all"), 0.7)[1] == 3           # deleted ones hidden
        assert queries.mails(db, queries.MailFilter(period="all", show_gone=True), 0.7)[1] == 5
    finally:
        db.close()


def test_reconcile_dry_run_and_reappearing_mail(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, MAILS)
    assert reconcile.run_reconcile(CFG, CREDS, tmp_path, live=False)["gone"] == 2  # "Trash" skipped by name, too
    store = Store(tmp_path / "data" / "state.db")
    assert not any(g for _, _, g in store.locations())
    store.set_gone(["<kept@x>"], True)
    store.close()
    assert reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)["back"] == 1


def test_inbox_stored_as_null(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "<a@x>", "INBOX", confidence=0.3)
    store.close()
    store = Store(tmp_path / "data" / "state.db")  # reopening normalizes old "INBOX" rows
    assert store.locations() == [("<a@x>", None, 0)]
    store.close()
    db = queries.connect(tmp_path)
    try:
        assert queries.folders(db) == [] and queries.stats(db, 0.7).uncertain == 1
    finally:
        db.close()


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
