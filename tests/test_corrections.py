from datetime import datetime
from types import SimpleNamespace

from email_sorter import i18n, imap, manual, reconcile
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
    monkeypatch.setattr(imap, "MailBox", lambda *a, **kw: FakeMailBox(folders))
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


def test_star_by_hand(tmp_path, monkeypatch):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "a", "werbung", "INBOX/Werbung")
    store.close()
    calls = []

    class FakeMB:
        folder = SimpleNamespace(exists=lambda name: True, set=lambda name: calls.append(("select", name)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def flag(self, uids, flag, value):
            calls.append(("flag", uids, flag, value))

    monkeypatch.setattr(manual, "connect", lambda cfg, creds: FakeMB())
    monkeypatch.setattr(manual, "delimiter", lambda mb: ".")
    monkeypatch.setattr(manual, "find_uids", lambda mb, folder, wanted, fallback_days:
                        calls.append(("find", folder)) or {"a": "7"})
    manual.set_star(CFG, CREDS, tmp_path, "a", True)
    from imap_tools import MailMessageFlags
    assert ("find", "INBOX.Werbung") in calls and ("flag", ["7"], MailMessageFlags.FLAGGED, True) in calls  # in its folder
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["flagged"] == 1
    store.close()
    manual.set_star(CFG, CREDS, tmp_path, "a", False)
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["flagged"] == 0 and calls[-2][-1] is False
    store.close()


class BatchMailbox:
    """A fake mailbox for the batch functions: which folders exist, which mails a folder search finds, and what was
    moved or flagged, with how many IMAP sessions were opened."""

    def __init__(self, monkeypatch, folders, missing=(), failing=()):
        self.sessions, self.moves, self.flags, self.created = 0, [], [], []
        self.folders, self.missing, self.failing = set(folders), set(missing), set(failing)
        me = self

        class Session:
            folder = SimpleNamespace(exists=lambda name: name in me.folders, set=lambda name: None)

            def __enter__(self):
                me.sessions += 1
                return self

            def __exit__(self, *exc):
                return False

            def flag(self, uids, flag, value):
                me.flags.append((list(uids), value))

        monkeypatch.setattr(manual, "connect", lambda cfg, creds: Session())
        monkeypatch.setattr(manual, "delimiter", lambda mb: ".")
        monkeypatch.setattr(manual, "find_uids", lambda mb, folder, wanted, fallback_days: {
            k: f"uid-{k}" for k in wanted if k not in me.missing})
        monkeypatch.setattr(manual, "ensure_folder", lambda mb, name: me.created.append(name))
        monkeypatch.setattr(manual, "move_uids", self.move)

    def move(self, mb, uids, folder):
        if folder in self.failing:
            raise RuntimeError("the server said no")
        self.moves.append((folder, list(uids)))


def _batch_store(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "a", "werbung", None, confidence=0.4)           # uncertain, suggested werbung
    _record(store, "b", "finanzen", None, confidence=0.4)          # uncertain, suggested finanzen
    _record(store, "c", "persoenlich", None, confidence=0.4)       # its category has no folder
    _record(store, "d", "werbung", "INBOX/Werbung")                # sorted already
    _record(store, "f", "werbung", "INBOX/Werbung")                # sorted, confident: a correction when moved
    store.close()


def test_accepting_several_suggestions_moves_per_folder_in_one_session(tmp_path, monkeypatch):
    _batch_store(tmp_path)
    mb = BatchMailbox(monkeypatch, {"INBOX", "INBOX.Werbung", "INBOX.Finanzen"})
    out = manual.accept_mails(CFG, CREDS, tmp_path, ["a", "b", "c", "d", "x"])
    # a and b go to the folders of their suggestions; c has no folder, d is sorted, x is not in the log
    assert out == {"a": manual.MOVED, "b": manual.MOVED, "c": manual.NO_FOLDER, "d": manual.SORTED, "x": manual.UNKNOWN}
    assert sorted(mb.moves) == [("INBOX.Finanzen", ["uid-b"]), ("INBOX.Werbung", ["uid-a"])] and mb.sessions == 1
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["moved_to"] == "INBOX/Werbung" and store.get("b")["moved_to"] == "INBOX/Finanzen"
    assert store.get("c")["moved_to"] is None and store.get("a")["source"] == "manual"
    store.close()
    # nothing to do on the server: no session at all
    assert manual.accept_mails(CFG, CREDS, tmp_path, ["c", "d"]) == {"c": manual.NO_FOLDER, "d": manual.SORTED}
    assert mb.sessions == 1


def test_moving_several_mails_counts_corrections_and_reports_each(tmp_path, monkeypatch):
    _batch_store(tmp_path)
    mb = BatchMailbox(monkeypatch, {"INBOX", "INBOX.Werbung", "INBOX.Finanzen"}, missing={"b"})
    out = manual.move_mails(CFG, CREDS, tmp_path, {"a": "finanzen", "b": "finanzen", "d": "werbung", "f": "finanzen"})
    # a and f moved, b is no longer in its folder, d is already where it should be
    assert out == {"a": manual.MOVED, "b": manual.GONE, "d": manual.UNCHANGED, "f": manual.MOVED}
    # one move per folder the mails are in (the inbox, the folder of f), in one session
    assert sorted(mb.moves) == [("INBOX.Finanzen", ["uid-a"]), ("INBOX.Finanzen", ["uid-f"])] and mb.sessions == 1
    store = Store(tmp_path / "data" / "state.db")
    # a correction of the model's confident decision counts for the correction rate, as for a single mail
    assert store.correction("f")["corrected_to"] == "finanzen" and store.correction("a") is None
    assert store.get("b")["moved_to"] is None  # not found: the log is as it was
    store.close()


def test_a_failed_move_and_a_missing_folder_leave_the_rest_alone(tmp_path, monkeypatch):
    _batch_store(tmp_path)
    mb = BatchMailbox(monkeypatch, {"INBOX", "INBOX.Werbung"}, failing={"INBOX.Finanzen"})
    out = manual.move_mails(CFG, CREDS, tmp_path, {"a": "werbung", "b": "finanzen"})
    assert out["a"] == manual.MOVED and out["b"] == "failed:the server said no" and mb.moves == [("INBOX.Werbung", ["uid-a"])]
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("b")["moved_to"] is None
    store.close()
    # the folder a mail is in is gone: not found, without asking the server for it
    gone = BatchMailbox(monkeypatch, {"INBOX"})
    assert manual.move_mails(CFG, CREDS, tmp_path, {"d": "inbox"}) == {"d": manual.GONE} and gone.moves == []


def test_starring_several_mails_per_folder(tmp_path, monkeypatch):
    _batch_store(tmp_path)
    mb = BatchMailbox(monkeypatch, {"INBOX", "INBOX.Werbung"}, missing={"d"})
    out = manual.set_stars(CFG, CREDS, tmp_path, ["a", "d", "f", "x"], True)
    assert out == {"a": manual.MOVED, "d": manual.GONE, "f": manual.MOVED, "x": manual.UNKNOWN} and mb.sessions == 1
    assert sorted(mb.flags) == [(["uid-a"], True), (["uid-f"], True)]  # a in the inbox, f in its folder
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["flagged"] == 1 and store.get("f")["flagged"] == 1 and store.get("d")["flagged"] == 0
    store.close()
    manual.set_stars(CFG, CREDS, tmp_path, ["a"], False)
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["flagged"] == 0
    store.close()
