import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from email_sorter.expiry import find_deadline, resolve_expiry, window_deadline
from email_sorter.jev import Decision, build_request, parse_response
from email_sorter.store import GONE, MOVED, Store

SENT = date(2026, 9, 23)  # a Wednesday


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Gutschein gültig bis 30.09.", date(2026, 9, 30)),
        ("Aktion läuft bis zum 30.09.2026, solange Vorrat reicht", date(2026, 9, 30)),
        ("Nur bis einschließlich Sonntag, 27.9. bestellen", date(2026, 9, 27)),
        ("Das Angebot endet am 1. Oktober", date(2026, 10, 1)),
        ("Sale ends October 3rd", date(2026, 10, 3)),
        ("Offer valid until 5 October 2026", date(2026, 10, 5)),
        ("Dein Gutschein läuft am 02.10.26 ab", date(2026, 10, 2)),
        # earliest of several deadlines wins
        ("Frühbucher bis 25.09., regulär bis 15.10.", date(2026, 9, 25)),
    ],
)
def test_find_deadline(text, expected):
    assert find_deadline(text, SENT) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Bis zu 40 % Rabatt auf alles",           # "bis" without a date
        "Bis 0:00 ist Schuh-Zeit!",               # time, not a date
        "Ihre Bestellung vom 12.09. ist unterwegs",  # date without a deadline cue
        "Gültig bis 01.03.2025",                  # long past -> not a deadline for this mail
        "Mehr als 10.000 Artikel bis 20 Uhr",      # thousands separator
    ],
)
def test_find_deadline_ignores_non_deadlines(text):
    assert find_deadline(text, SENT) is None


def test_find_deadline_rolls_over_new_year():
    assert find_deadline("Angebot gültig bis 05.01.", date(2026, 12, 28)) == date(2027, 1, 5)


def test_window_deadline():
    assert window_deadline("same_day", SENT) == SENT
    assert window_deadline("one_two_days", SENT) == date(2026, 9, 25)
    assert window_deadline("within_week", SENT) == date(2026, 9, 30)
    assert window_deadline("later_or_unknown", SENT) is None
    assert window_deadline(None, SENT) is None


def test_explicit_date_beats_window():
    assert resolve_expiry("Nur bis 24.09.!", SENT, "within_month") == date(2026, 9, 24)
    assert resolve_expiry("Nur heute!", SENT, "same_day") == SENT


def test_request_and_response_include_expiry_questions():
    req = build_request("m", {}, {"newsletter": "x", "other": "y"})
    assert req["questions"]["has_expiry"]["type"] == "noul"
    assert "one_two_days" in req["questions"]["expiry_window"]["criteria"]

    resp = {
        "answers": {
            "category": {"choice": "newsletter", "confidence": 0.99, "probabilities": {"newsletter": 0.99}},
            "needs_action": {"noul": 0.8},
            "has_expiry": {"noul": 0.93},
            "expiry_window": {"choice": "one_two_days", "confidence": 0.7},
        }
    }
    d = parse_response(resp, {"newsletter": "x", "other": "y"})
    assert (d.has_expiry, d.expiry_window) == (0.93, "one_two_days")


def test_unknown_window_is_ignored():
    resp = {"answers": {"category": {"choice": "a", "confidence": 1}, "expiry_window": {"choice": "bogus"}}}
    assert parse_response(resp, {"a": "x", "b": "y"}).expiry_window is None


def _outcome(key, category="werbung", expires=None, folder="INBOX/Werbung"):
    decision = Decision(category, 1.0, {}, 0.0, 0.0)
    return SimpleNamespace(key=key, received="2026-09-20T10:00+02:00", sender="s", subject="x",
                           decision=decision, folder=folder, flag=False, expires=expires)


def test_store_due_and_tagging(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record(_outcome("past", expires=date(2026, 9, 22)))
    store.record(_outcome("today", expires=date(2026, 9, 23)))   # still valid today
    store.record(_outcome("none"))
    due = store.due_expired(date(2026, 9, 23))
    assert [k for k, _, _ in due] == ["past"]
    store.mark_expired(["past"], MOVED)
    assert store.due_expired(date(2026, 9, 23)) == []
    assert [k for k, _, _ in store.due_expired(date(2026, 9, 24))] == ["today"]
    store.mark_expired(["today"], GONE)
    assert store.due_expired(date(2026, 9, 30)) == []
    store.close()


def test_store_migrates_old_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE processed (message_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL,
                  received TEXT, sender TEXT, subject TEXT, category TEXT NOT NULL, confidence REAL NOT NULL,
                  needs_action REAL NOT NULL, moved_to TEXT, flagged INTEGER NOT NULL, cost_usd REAL NOT NULL)""")
    db.execute("INSERT INTO processed VALUES ('k1','t','2026-09-20T10:00+02:00','s','x','newsletter',1,0,"
               "'INBOX/Newsletter',0,0)")
    db.execute("INSERT INTO processed VALUES ('k2','t',NULL,'s','x','finanzen',1,0,'INBOX/Finanzen',0,0)")
    db.commit()
    db.close()

    store = Store(path)
    assert store.unchecked_expiry(["newsletter"]) == [("k1", "INBOX/Newsletter", "2026-09-20T10:00+02:00")]
    store.set_expiry("k1", date(2026, 9, 21))
    assert store.unchecked_expiry(["newsletter"]) == []
    assert [k for k, _, _ in store.due_expired(date(2026, 9, 23))] == ["k1"]
    # new records count as checked
    store.record(_outcome("k3"))
    assert store.unchecked_expiry(["newsletter"]) == []
    store.close()


class _Head:
    def __init__(self, uid, key):
        self.uid, self.headers = uid, {"message-id": (key,)}


class _FakeFolder:
    def __init__(self):
        self.selected, self.created = [], []

    def list(self):
        return [SimpleNamespace(name="INBOX", delim=".")]

    def set(self, name):
        self.selected.append(name)

    def exists(self, name):
        return False

    def create(self, name):
        self.created.append(name)

    def subscribe(self, name, value):
        pass


class _FakeMailBox:
    def __init__(self, heads):
        self.folder, self.heads, self.flags, self.moves = _FakeFolder(), heads, [], []

    def fetch(self, *args, **kwargs):
        return iter(self.heads)

    def flag(self, uids, keyword, value):
        self.flags.append((sorted(uids), keyword, value))

    def move(self, uids, folder):
        self.moves.append((sorted(uids), folder))


def test_move_expired_moves_and_marks_missing(tmp_path):
    from email_sorter import sorter
    from email_sorter.config import load_config
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "config.toml")
    store = Store(tmp_path / "s.db")
    store.record(_outcome("<a@x>", expires=date(2026, 9, 23)))
    store.record(_outcome("<gone@x>", expires=date(2026, 9, 23)))
    store.record(_outcome("<later@x>", expires=date(2026, 10, 1)))
    mb = _FakeMailBox([_Head("7", "<a@x>"), _Head("8", "<later@x>")])

    assert sorter.move_expired(mb, cfg, store, today=date(2026, 9, 25)) == 1
    assert mb.flags == []  # no IMAP keywords any more
    assert mb.moves == [(["7"], "INBOX.Werbung.Abgelaufen")]
    assert mb.folder.created == ["INBOX.Werbung.Abgelaufen"]
    assert mb.folder.selected == ["INBOX.Werbung", "INBOX"]
    assert store.due_expired(date(2026, 9, 25)) == []  # moved + gone are both settled
    store.close()


def test_move_expired_splits_long_uid_lists(tmp_path, monkeypatch):
    from email_sorter import sorter
    from email_sorter.config import load_config
    from pathlib import Path

    monkeypatch.setattr(sorter, "UID_CHUNK", 2)
    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "config.toml")
    store = Store(tmp_path / "s.db")
    for i in range(5):
        store.record(_outcome(f"<k{i}@x>", expires=date(2026, 9, 1)))
    mb = _FakeMailBox([_Head(str(10 + i), f"<k{i}@x>") for i in range(5)])
    assert sorter.move_expired(mb, cfg, store, today=date(2026, 9, 25)) == 5
    assert [len(u) for u, _ in mb.moves] == [2, 2, 1]
    store.close()


def test_move_expired_does_nothing_without_expired_folder(tmp_path):
    from email_sorter import sorter
    from email_sorter.config import Config, load_config
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "config.toml")
    cfg = Config(**{**cfg.__dict__, "expired_folder": None})
    store = Store(tmp_path / "s.db")
    store.record(_outcome("<a@x>", expires=date(2026, 9, 1)))
    mb = _FakeMailBox([_Head("7", "<a@x>")])
    assert sorter.move_expired(mb, cfg, store, today=date(2026, 9, 25)) == 0
    assert mb.moves == []
    assert len(store.due_expired(date(2026, 9, 25))) == 1  # kept until a folder is configured
    store.close()


def test_move_expired_uses_category_folder_over_default(tmp_path):
    from dataclasses import replace
    from email_sorter import sorter
    from email_sorter.config import Config, load_config
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "config.toml")
    cats = dict(cfg.categories)
    cats["unterlagen"] = replace(cats["unterlagen"], expired_folder="INBOX/Unterlagen/Abgelaufen")
    cfg = Config(**{**cfg.__dict__, "categories": cats})
    store = Store(tmp_path / "s.db")
    store.record(_outcome("<w@x>", category="werbung", expires=date(2026, 9, 1)))
    store.record(_outcome("<u@x>", category="unterlagen", expires=date(2026, 9, 1), folder="INBOX/Werbung"))
    mb = _FakeMailBox([_Head("7", "<w@x>"), _Head("8", "<u@x>")])
    assert sorter.move_expired(mb, cfg, store, today=date(2026, 9, 25)) == 2
    assert sorted(f for _, f in mb.moves) == ["INBOX.Unterlagen.Abgelaufen", "INBOX.Werbung.Abgelaufen"]
    store.close()
