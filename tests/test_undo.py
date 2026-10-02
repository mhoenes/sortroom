from datetime import datetime
from types import SimpleNamespace

from email_sorter import imap, undo
from email_sorter.classifier import Decision
from email_sorter.config import Credentials
from email_sorter.sorter import RunResult
from email_sorter.store import MOVED, Store
from email_sorter.web import queries
from support import example_config

CFG = example_config()
CREDS = Credentials("u", "p", "k")
RUN = datetime(2026, 9, 30, 10, 0, 0)
LATER = datetime(2026, 9, 30, 11, 0, 0)


class Head:
    def __init__(self, uid, key):
        self.uid, self.headers = uid, {"message-id": (key,)}


class FakeMailBox:
    """Folders of mails by message key; UIDs change on every move, like on a real server."""

    def __init__(self, folders, starred=()):
        self.folders = {name: {str(i): key for i, key in enumerate(keys, 1)} for name, keys in folders.items()}
        self.starred = set(starred)  # message keys
        self.selected, self.next_uid = None, 1000
        self.folder = SimpleNamespace(
            list=lambda: [SimpleNamespace(name="INBOX", delim=".")],
            exists=lambda n: n in self.folders,
            set=lambda n: setattr(self, "selected", n),
            create=lambda n: self.folders.setdefault(n, {}),
            subscribe=lambda n, v: None)

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, *a, **kw):
        return [Head(uid, key) for uid, key in self.folders[self.selected].items()]

    def flag(self, uids, flag, value):
        for uid in uids:
            (self.starred.add if value else self.starred.discard)(self.folders[self.selected][uid])

    def move(self, uids, target):
        for uid in uids:
            self.next_uid += 1
            self.folders[target][str(self.next_uid)] = self.folders[self.selected].pop(uid)

    def where(self, key):
        return next(name for name, mails in self.folders.items() if key in mails.values())


def _record(store, key, category, folder, flag=False, confidence=0.9):
    store.record(SimpleNamespace(key=key, received="2026-09-20T10:00+02:00", sender="s", subject=key,
                                 decision=Decision(category, confidence, {}, 0.0, 0.0), folder=folder,
                                 flag=flag, expires=None))


def _run(store, started, kind="run", detail=None):
    store.record_run(kind, detail, started, started, RunResult(exit_code=0, live=True, classified=1))


def _setup(tmp_path, monkeypatch, folders, starred=()):
    fake = FakeMailBox(folders, starred)
    monkeypatch.setattr(imap, "MailBox", lambda *a, **kw: fake)
    store = Store(tmp_path / "data" / "state.db")
    return fake, store


def _sorted_by_a_run(tmp_path, monkeypatch):
    """A run filed a (starred) and c, and left b in the inbox."""
    fake, store = _setup(tmp_path, monkeypatch,
                         {"INBOX": ["b"], "INBOX.Finanzen": ["a"], "INBOX.Reisen": ["c"]}, starred={"a"})
    store.track(RUN)
    _record(store, "a", "finanzen", "INBOX/Finanzen", flag=True)
    _record(store, "b", "reisen", None, confidence=0.3)
    _record(store, "c", "reisen", "INBOX/Reisen")
    _run(store, RUN)
    return fake, store


def test_undo_moves_back_removes_stars_and_leaves_the_mails_in_the_inbox(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.set_manual("c", "finanzen", "INBOX/Finanzen")  # corrected by hand since: left alone
    store.close()

    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True)
    assert result["ok"] and (result["moved"], result["unflagged"], result["skipped"]) == (1, 1, 1)
    assert fake.where("a") == "INBOX" and fake.starred == set() and fake.where("c") == "INBOX.Reisen"

    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["moved_to"] is None and store.get("a")["flagged"] == 0
    assert store.is_processed("a") and store.is_processed("b")  # not sorted again
    assert store.get("c")["moved_to"] == "INBOX/Finanzen"
    assert store.undoable_runs() == []
    last = store.recent_runs(1)[0]
    assert (last["kind"], last["detail"], last["moved"]) == ("undo", RUN.isoformat(), 1)
    store.close()
    again = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True)
    assert not again["ok"] and again["exit_code"] == 2


def test_undo_with_sort_again_drops_the_mails_from_the_log(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, "last", live=True, sort_again=True)
    assert result["moved"] == 2 and fake.where("c") == "INBOX"
    store = Store(tmp_path / "data" / "state.db")
    assert not any(store.is_processed(k) for k in "abc")
    store.close()


def test_undo_dry_run_changes_nothing(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=False)
    assert result["ok"] and (result["moved"], result["unflagged"]) == (2, 1)
    assert fake.where("a") == "INBOX.Finanzen" and fake.starred == {"a"}
    store = Store(tmp_path / "data" / "state.db")
    assert store.undoable_runs() == [RUN.isoformat()] and store.get("a")["moved_to"] == "INBOX/Finanzen"
    store.close()


def test_mails_moved_deleted_or_expired_since_are_skipped(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    del fake.folders["INBOX.Reisen"]["1"]            # c deleted by you
    fake.folders["INBOX.Finanzen"].pop("1")          # a moved elsewhere by you
    fake.folders["INBOX"]["9"] = "a"
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True)
    assert (result["moved"], result["skipped"]) == (0, 2)
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("a")["moved_to"] == "INBOX/Finanzen"  # the log keeps what it knew
    store.close()

    fake, store = _sorted_by_a_run(tmp_path / "x", monkeypatch)
    store.mark_expired(["c"], MOVED)
    store.close()
    assert undo.run_undo(CFG, CREDS, tmp_path / "x", RUN.isoformat(), live=True)["skipped"] == 1
    assert fake.where("c") == "INBOX.Reisen"


def test_undo_of_a_resort_restores_folder_and_category(tmp_path, monkeypatch):
    fake, store = _setup(tmp_path, monkeypatch, {"INBOX": [], "INBOX.Finanzen": ["d"], "INBOX.Reisen": ["e"]})
    _record(store, "d", "reisen", "INBOX/Reisen", confidence=0.8)  # sorted long ago
    store.track(RUN, "INBOX/Reisen")
    _record(store, "d", "finanzen", "INBOX/Finanzen")               # the re-sort moved it
    _record(store, "e", "reisen", "INBOX/Reisen")                   # and left this one
    _run(store, RUN, "resort", "INBOX/Reisen")
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True, sort_again=True)
    assert result["moved"] == 1 and fake.where("d") == "INBOX.Reisen"
    store = Store(tmp_path / "data" / "state.db")
    d = store.get("d")
    assert (d["category"], d["moved_to"], d["confidence"]) == ("reisen", "INBOX/Reisen", 0.8)
    assert not store.is_processed("e")  # unknown before the re-sort: sorted again
    store.close()


def test_a_later_run_takes_over_the_mail(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.track(LATER, "INBOX/Finanzen")
    _record(store, "a", "finanzen", "INBOX/Finanzen")  # re-sorted, stays
    _run(store, LATER, "resort", "INBOX/Finanzen")
    assert store.undoable_runs() == [LATER.isoformat(), RUN.isoformat()]
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True)
    assert result["skipped"] == 1 and fake.where("a") == "INBOX.Finanzen"


def test_dry_runs_and_untracked_records_cannot_be_undone(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "a", "finanzen", "INBOX/Finanzen")  # no track(): e.g. a dry run records nothing anyway
    store.record_run("run", None, RUN, RUN, RunResult(exit_code=0, live=False, classified=1))
    assert store.undoable_runs() == []
    store.close()


def test_rename_folder_keeps_undo_in_step(tmp_path):
    store = Store(tmp_path / "s.db")
    store.track(RUN, "INBOX/Reisen/Alt")
    _record(store, "a", "finanzen", "INBOX/Reisen/Neu")
    store.rename_moved_to("INBOX/Reisen", "INBOX/Urlaub")
    entry = store.undo_entries(RUN.isoformat())[0]
    assert (entry["origin"], entry["moved_to"], entry["row"]["moved_to"]) == (
        "INBOX/Urlaub/Alt", "INBOX/Urlaub/Neu", "INBOX/Urlaub/Neu")
    store.close()


def test_queries_list_undoable_runs(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    store.track(RUN)
    _record(store, "a", "finanzen", "INBOX/Finanzen")
    _record(store, "b", "finanzen", "INBOX/Finanzen")
    _run(store, RUN)
    _run(store, LATER)
    store.close()
    db = queries.connect(tmp_path)
    try:
        assert queries.undoable_runs(db) == [{"started": RUN.isoformat(), "kind": "run", "detail": None, "mails": 2}]
        runs = queries.recent_runs(db, skip_empty=False)
        assert [(r["started"], r["undoable"]) for r in runs] == [(LATER.isoformat(), 0), (RUN.isoformat(), 1)]
    finally:
        db.close()


def test_undo_only_the_chosen_mails(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.close()
    result = undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True, keys=["c"])
    assert result["ok"] and (result["moved"], result["unflagged"]) == (1, 0)
    assert fake.where("c") == "INBOX" and fake.where("a") == "INBOX.Finanzen" and fake.starred == {"a"}
    store = Store(tmp_path / "data" / "state.db")
    assert store.undoable_runs() == [RUN.isoformat()]                         # a and b can still be undone
    assert {e["key"] for e in store.undo_entries(RUN.isoformat())} == {"a", "b"}
    store.close()
    assert undo.run_undo(CFG, CREDS, tmp_path, RUN.isoformat(), live=True, keys=["x"])["exit_code"] == 2


def test_the_run_page_lists_its_mails_and_why_some_cannot_be_undone(tmp_path, monkeypatch):
    fake, store = _sorted_by_a_run(tmp_path, monkeypatch)
    store.set_manual("c", "finanzen", "INBOX/Finanzen")
    store.close()
    db = queries.connect(tmp_path)
    mails = {m["key"]: m for m in queries.run_mails(db, RUN.isoformat())}
    db.close()
    assert set(mails) == {"a", "b", "c"} and mails["a"]["starred"] and not mails["c"]["starred"]
    assert (mails["a"]["origin"], mails["a"]["run_moved_to"], mails["b"]["run_moved_to"]) == (None, "INBOX/Finanzen", None)
    why = {k: undo.changed_since(m if m["known"] else None, bool(m["later"]), m["run_moved_to"]) for k, m in mails.items()}
    assert why == {"a": None, "b": None, "c": "manual"}
