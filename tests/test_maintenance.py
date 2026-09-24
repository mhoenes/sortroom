from types import SimpleNamespace

from email_sorter import maintenance
from email_sorter.config import Credentials, load_config
from email_sorter.jev import Decision
from email_sorter.store import Store
from pathlib import Path

CFG = load_config(Path(__file__).resolve().parent.parent / "config.toml")
CREDS = Credentials("u", "p", "k")


def _record(store, key, category, moved_to):
    store.record(SimpleNamespace(key=key, received="2026-09-20T10:00+02:00", sender="s", subject="x",
                                 decision=Decision(category, 1.0, {}, 0.0, 0.0), folder=moved_to,
                                 flag=False, expires=None))


def test_store_rename_category(tmp_path):
    store = Store(tmp_path / "s.db")
    _record(store, "a", "postfach", None)
    _record(store, "b", "postfach", None)
    _record(store, "c", "finanzen", "INBOX/Finanzen")
    assert store.count_category("postfach") == 2
    store.rename_category("postfach", "portal")
    assert store.count_category("postfach") == 0 and store.count_category("portal") == 2
    assert store.count_category("finanzen") == 1
    store.close()


def test_store_rename_moved_to_includes_subfolders_only(tmp_path):
    store = Store(tmp_path / "s.db")
    _record(store, "a", "werbung", "INBOX/Newsletter")
    _record(store, "b", "werbung", "INBOX/Newsletter/Abgelaufen")
    _record(store, "c", "werbung", "INBOX/Newsletter_Alt")   # similar name, not a subfolder
    _record(store, "d", "finanzen", "INBOX/Finanzen")
    assert store.count_moved_to("INBOX/Newsletter") == 2
    store.rename_moved_to("INBOX/Newsletter", "INBOX/Werbung")
    rows = dict(store.db.execute("SELECT message_key, moved_to FROM processed"))
    assert rows == {"a": "INBOX/Werbung", "b": "INBOX/Werbung/Abgelaufen",
                    "c": "INBOX/Newsletter_Alt", "d": "INBOX/Finanzen"}
    store.close()


def test_rename_category_dry_run_changes_nothing(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "a", "verdacht", "INBOX/Verdacht")
    store.close()
    result = maintenance.rename_category("verdacht", "verdaechtig", live=False, base_dir=tmp_path)
    assert result.classified == 1
    store = Store(tmp_path / "data" / "state.db")
    assert store.count_category("verdacht") == 1
    store.close()


class FakeFolders:
    def __init__(self, names):
        self.names = dict(names)  # name -> mail count
        self.subscribed, self.renames = set(names), []

    def list(self, pattern=""):
        return [SimpleNamespace(name=n, delim=".") for n in self.names if n.startswith(pattern) or pattern == ""]

    def exists(self, name):
        return name in self.names

    def status(self, name, items):
        return {"MESSAGES": self.names[name]}

    def rename(self, old, new):
        self.renames.append((old, new))
        self.names = {(new + n[len(old):] if n == old or n.startswith(old + ".") else n): c
                      for n, c in self.names.items()}

    def subscribe(self, name, value):
        (self.subscribed.add if value else self.subscribed.discard)(name)


class FakeMailBox:
    def __init__(self, folders):
        self.folder = folders

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _setup(tmp_path, monkeypatch, folders):
    fake = FakeMailBox(FakeFolders(folders))
    monkeypatch.setattr(maintenance, "MailBox", lambda *a, **kw: fake)
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "a", "werbung", "INBOX/Newsletter")
    _record(store, "b", "werbung", "INBOX/Newsletter/Abgelaufen")
    store.close()
    return fake


def test_rename_folder_live_renames_tree_subscriptions_and_log(tmp_path, monkeypatch):
    fake = _setup(tmp_path, monkeypatch, {"INBOX": 5, "INBOX.Newsletter": 100, "INBOX.Newsletter.Abgelaufen": 20})
    result = maintenance.rename_folder(CFG, CREDS, "INBOX/Newsletter", "INBOX/Werbung", live=True, base_dir=tmp_path)
    assert result.exit_code == 0 and result.moved == 120 and result.classified == 2
    assert fake.folder.renames == [("INBOX.Newsletter", "INBOX.Werbung")]
    assert {"INBOX.Werbung", "INBOX.Werbung.Abgelaufen"} <= fake.folder.subscribed
    assert not {"INBOX.Newsletter", "INBOX.Newsletter.Abgelaufen"} & fake.folder.subscribed
    store = Store(tmp_path / "data" / "state.db")
    assert store.count_moved_to("INBOX/Werbung") == 2 and store.count_moved_to("INBOX/Newsletter") == 0
    store.close()


def test_rename_folder_dry_run_changes_nothing(tmp_path, monkeypatch):
    fake = _setup(tmp_path, monkeypatch, {"INBOX": 5, "INBOX.Newsletter": 100})
    result = maintenance.rename_folder(CFG, CREDS, "INBOX/Newsletter", "INBOX/Werbung", live=False, base_dir=tmp_path)
    assert result.exit_code == 0 and result.moved == 100
    assert fake.folder.renames == []


def test_rename_folder_refuses_existing_target(tmp_path, monkeypatch):
    fake = _setup(tmp_path, monkeypatch, {"INBOX": 5, "INBOX.Newsletter": 1, "INBOX.Werbung": 1})
    result = maintenance.rename_folder(CFG, CREDS, "INBOX/Newsletter", "INBOX/Werbung", live=True, base_dir=tmp_path)
    assert result.exit_code == 2 and fake.folder.renames == []
