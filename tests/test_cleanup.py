import dataclasses
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from email_sorter import cleanup, imap
from email_sorter.classifier import Decision
from email_sorter.config import EXAMPLE_MAILBOXES, ConfigError, Credentials, DeleteRule, _read_toml, config_from_raw
from email_sorter.store import Store
from support import example_config

CREDS = Credentials("u", "p", "k")
TODAY = date(2026, 10, 3)


def _cfg(*rules):
    return dataclasses.replace(example_config(), delete_rules=tuple(rules))


class Mail:
    def __init__(self, uid, key, day, seen=True, flagged=False):
        self.uid, self.headers, self.date = uid, {"message-id": (key,)}, datetime(2026, *day)
        self.seen, self.flagged, self.from_, self.subject = seen, flagged, "s@x", key


class FakeMailBox:
    """Folders of mails; the search is the rule itself (see the criteria monkeypatch)."""

    def __init__(self, folders, specials=None):
        self.folders = folders
        self.selected = None
        listed = [SimpleNamespace(name=n, delim="/", flags=(specials or {}).get(n, ())) for n in folders]
        self.folder = SimpleNamespace(list=lambda *a: listed, exists=lambda n: n in folders,
                                      set=lambda n: setattr(self, "selected", n))

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, rule, limit=None, **kw):
        cut = datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=rule.days)
        found = [m for m in self.folders[self.selected] if m.date < cut and (m.seen or not rule.only_read)
                 and (rule.starred or not m.flagged)]
        return found[:limit]

    def move(self, uids, target):
        mails = [m for m in self.folders[self.selected] if m.uid in uids]
        self.folders[self.selected] = [m for m in self.folders[self.selected] if m.uid not in uids]
        self.folders[target].extend(mails)

    def keys(self, folder):
        return sorted(m.headers["message-id"][0] for m in self.folders[folder])


@pytest.fixture
def mailbox(monkeypatch):
    fake = FakeMailBox({
        "INBOX": [Mail("1", "inbox-old", (8, 1))],
        "INBOX/Promotions": [Mail("1", "old", (8, 1)), Mail("2", "old-unread", (8, 2), seen=False),
                             Mail("3", "old-starred", (8, 3), flagged=True), Mail("4", "new", (10, 1))],
        "Papierkorb": [], "Sent": [], "Archive": [Mail("1", "archived", (1, 1))]},
        specials={"Sent": ("\\Sent",)})
    monkeypatch.setattr(imap, "MailBox", lambda *a, **kw: fake)
    monkeypatch.setattr(cleanup, "criteria", lambda rule, today: rule)  # the fake searches by the rule
    return fake


def test_old_mail_goes_to_the_trash_and_leaves_the_log(tmp_path, mailbox):
    store = Store(tmp_path / "data" / "state.db")
    store.record(SimpleNamespace(key="old", received="2026-08-01T10:00+02:00", sender="s", subject="old",
                                 decision=Decision("werbung", 0.9, {}, 0.0, 0.0), folder="INBOX/Promotions",
                                 flag=False, expires=None))
    store.close()
    cfg = _cfg(DeleteRule("INBOX/Promotions", 30))
    dry = cleanup.run_cleanup(cfg, CREDS, tmp_path, live=False, today=TODAY)
    assert dry["moved"] == 2 and mailbox.keys("Papierkorb") == []        # old and old-unread, not the starred one
    result = cleanup.run_cleanup(cfg, CREDS, tmp_path, live=True, today=TODAY)
    assert result["ok"] and result["moved"] == 2
    assert mailbox.keys("Papierkorb") == ["old", "old-unread"] and mailbox.keys("INBOX/Promotions") == ["new", "old-starred"]
    store = Store(tmp_path / "data" / "state.db")
    assert store.get("old")["gone"] == 1
    last = store.recent_runs(1)[0]
    assert (last["kind"], last["moved"]) == ("cleanup", 2)
    store.close()


def test_rule_options_and_folders_that_are_never_emptied(tmp_path, mailbox):
    cfg = _cfg(DeleteRule("INBOX/Promotions", 30, only_read=True, starred=True), DeleteRule("Sent", 1),
               DeleteRule("Papierkorb", 1), DeleteRule("Missing", 1))
    result = cleanup.run_cleanup(cfg, CREDS, tmp_path, live=True, today=TODAY)
    assert mailbox.keys("Papierkorb") == ["old", "old-starred"]        # read only, starred too
    assert result["skipped"] == 3                                        # sent, the trash itself, missing


def test_without_a_trash_folder_nothing_runs(tmp_path, mailbox):
    mailbox.folders["Deleted"] = mailbox.folders.pop("Papierkorb")
    mailbox.folder.list = lambda *a: [SimpleNamespace(name=n, delim="/", flags=()) for n in mailbox.folders
                                      if n != "Deleted"] + [SimpleNamespace(name="Junk", delim="/", flags=())]
    result = cleanup.run_cleanup(_cfg(DeleteRule("INBOX/Promotions", 30)), CREDS, tmp_path, live=True, today=TODAY)
    assert result["exit_code"] == 2 and result["moved"] == 0 and mailbox.keys("INBOX/Promotions") != []


def test_the_trash_is_found_by_its_special_use_first():
    listed = [SimpleNamespace(name="Trash", delim="/", flags=()),
              SimpleNamespace(name="[Gmail]/Bin", delim="/", flags=("\\Trash",)),
              SimpleNamespace(name="[Gmail]/All Mail", delim="/", flags=("\\All",))]
    mb = SimpleNamespace(folder=SimpleNamespace(list=lambda *a: listed))
    trash, protected = cleanup.trash_and_protected(mb, "INBOX")
    assert trash == "[Gmail]/Bin" and {"INBOX", "Trash", "[Gmail]/Bin", "[Gmail]/All Mail"} <= protected


def test_criteria_search_by_arrival_and_leave_starred_and_unread_alone():
    text = str(cleanup.criteria(DeleteRule("X", 30), TODAY))
    assert "BEFORE 3-Sep-2026" in text and "UNFLAGGED" in text and "SEEN" not in text
    text = str(cleanup.criteria(DeleteRule("X", 7, only_read=True, starred=True), TODAY))
    assert "BEFORE 26-Sep-2026" in text and "SEEN" in text and "FLAGGED" not in text


def test_rules_in_the_settings_file():
    raw = {**_read_toml(EXAMPLE_MAILBOXES["de"]), "classifier": {"endpoint": "https://x", "model": "m"}}
    cfg = config_from_raw({**raw, "delete_rules": [{"folder": "INBOX/Werbung/", "days": 30, "only_read": True}]}, "t")
    assert cfg.delete_rules == (DeleteRule("INBOX/Werbung", 30, only_read=True),)
    for bad in ({"folder": "INBOX", "days": 3}, {"folder": "X", "days": 0}, {"folder": "", "days": 3}):
        with pytest.raises(ConfigError, match="deletion rule"):
            config_from_raw({**raw, "delete_rules": [bad]}, "t")
