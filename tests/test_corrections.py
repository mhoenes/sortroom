from datetime import datetime
from types import SimpleNamespace

from email_sorter import i18n, manual, reconcile
from email_sorter.classifier import Decision
from email_sorter.config import INBOX_ACTION, Credentials
from email_sorter.store import MOVED, Store
from email_sorter.web import queries
from support import HeaderFetch, example_config, raw_headers

CFG = example_config()
CREDS = Credentials("u", "p", "k")
NOW = datetime(2026, 9, 30, 12, 0)


def _record(store, key, category, folder, confidence=0.9, source="classifier", received="2026-09-20T10:00+02:00"):
    store.record(SimpleNamespace(key=key, received=received, sender="s", subject=key, source=source,
                                 decision=Decision(category, confidence, {}, 0.0, 0.0), folder=folder,
                                 flag=False, expires=None))


def _move(store, key, category):
    """What the Mails page does, without the IMAP part."""
    row = store.get(key)
    manual._note_correction(CFG, store, row, category)
    store.set_manual(key, row["category"] if category == INBOX_ACTION else category, manual.target_for(CFG, category))


def test_moves_on_the_mails_page(tmp_path):
    store = Store(tmp_path / "s.db")
    _record(store, "a", "werbung", "INBOX/Werbung")
    _record(store, "b", "werbung", "INBOX/Werbung")
    _record(store, "c", "werbung", None, confidence=0.4)            # uncertain: never decided
    _record(store, "d", "werbung", "INBOX/Werbung", source="rule")  # a sender rule, not the model
    _record(store, "e", "persoenlich", None)                          # stays in the inbox anyway
    _move(store, "a", "finanzen")
    _move(store, "b", INBOX_ACTION)
    _move(store, "c", "finanzen")
    _move(store, "d", "finanzen")
    _move(store, "e", INBOX_ACTION)
    rows = dict(store.db.execute("SELECT message_key, corrected_to FROM corrections"))
    assert rows == {"a": "finanzen", "b": INBOX_ACTION}
    _move(store, "a", "reisen")      # corrected again: still the model's werbung
    assert store.correction("a")["category"] == "werbung" and store.correction("a")["corrected_to"] == "reisen"
    _move(store, "a", "werbung")     # back to the model's choice: it stood after all
    assert store.correction("a") is None
    _record(store, "b", "finanzen", "INBOX/Finanzen")  # a new decision (re-sort) replaces the old one
    assert store.correction("b") is None
    store.close()


class FakeMailBox(HeaderFetch):
    def __init__(self, folders):
        self.folders = folders
        self.folder = SimpleNamespace(
            list=lambda: [SimpleNamespace(name=n, delim=".", flags=()) for n in folders],
            set=lambda n: setattr(self, "current", n))

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raw_mails(self):
        return [raw_headers(k) for k in self.folders[self.current]]


def test_reconcile_finds_mails_moved_by_hand(tmp_path, monkeypatch):
    store = Store(tmp_path / "data" / "state.db")
    for key in "abcdef":
        _record(store, key, "werbung", "INBOX/Werbung")
    _record(store, "g", "benachrichtigungen", "INBOX/Benachrichtigungen")
    _record(store, "h", "persoenlich", None)
    _record(store, "i", "werbung", "INBOX/Werbung")
    store.mark_expired(["i"], MOVED)
    _move(store, "f", "reisen")
    store.close()
    folders = {"INBOX": ["b", "f"], "INBOX.Werbung": ["a"], "INBOX.Finanzen": ["c", "h"], "INBOX.Archiv": ["d"],
               "INBOX.Benachrichtigungen": ["g"], "INBOX.Werbung.Abgelaufen": ["i"]}
    monkeypatch.setattr(reconcile, "MailBox", lambda *a, **kw: FakeMailBox(folders))
    reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)
    store = Store(tmp_path / "data" / "state.db")
    rows = {k: (c, how) for k, c, how in store.db.execute("SELECT message_key, corrected_to, how FROM corrections")}
    # a stays, b back in the inbox, c and h in another category's folder, d archived (neither), e deleted,
    # f corrected on the Mails page (a reconcile never overrides it), g in its shared folder, i expired
    assert rows == {"b": ("inbox", "reconcile"), "c": ("finanzen", "reconcile"), "h": ("finanzen", "reconcile"),
                    "f": ("reisen", "ui")}
    store.close()

    folders.update({"INBOX": ["f"], "INBOX.Werbung": ["a", "b"]})  # b moved back where it was put
    reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)        # h was filed: still corrected
    store = Store(tmp_path / "data" / "state.db")
    assert sorted(k for (k,) in store.db.execute("SELECT message_key FROM corrections")) == ["c", "f", "h"]
    store.close()


def test_correction_rate_per_category(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i in range(8):
        _record(store, f"w{i}", "werbung", "INBOX/Werbung")
    _record(store, "u", "werbung", None, confidence=0.4)                            # uncertain
    _record(store, "old", "werbung", "INBOX/Werbung", received="2026-01-01T10:00+01:00")
    _record(store, "f", "finanzen", "INBOX/Finanzen")
    _move(store, "w0", "finanzen")
    _move(store, "w1", INBOX_ACTION)
    store.set_gone(["w2"], True)  # deleted: counts neither way
    store.close()
    db = queries.connect(tmp_path)
    try:
        assert queries.corrections(db, 0.7, now=NOW) == {"werbung": (7, 2), "finanzen": (1, 0)}
    finally:
        db.close()


def test_percent():
    token = i18n.set_language("en")
    try:
        assert (i18n.percent(2, 7), i18n.percent(1, 3), i18n.percent(0, 5), i18n.percent(0, 0)) == ("29%", "33%", "0%", "–")
    finally:
        i18n.reset_language(token)
    assert i18n.percent(1, 40) == "2,5 %"
