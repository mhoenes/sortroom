import socket
from datetime import datetime
from types import SimpleNamespace

import pytest

from email_sorter import reconcile, unsubscribe
from email_sorter.classifier import Decision
from email_sorter.config import Credentials
from email_sorter.mailtext import unsubscribe_links
from email_sorter.store import Store
from email_sorter.web import queries
from support import HeaderFetch, example_config, raw_headers

CFG = example_config()
CREDS = Credentials("u", "p", "k")
NOW = datetime(2026, 9, 30, 12, 0)


def _msg(unsub=None, post=None, **kw):
    headers = {"message-id": (kw.pop("key", "<m@x>"),)}
    if unsub is not None:
        headers["list-unsubscribe"] = (unsub,)
    if post is not None:
        headers["list-unsubscribe-post"] = (post,)
    return SimpleNamespace(headers=headers, **kw)


def test_unsubscribe_links_from_the_headers():
    assert unsubscribe_links(_msg()) is None
    assert unsubscribe_links(_msg("<javascript:alert(1)>, <ftp://x>")) is None
    links, one_click = unsubscribe_links(_msg("<mailto:off@list.example?subject=unsubscribe>,\r\n <https://list.\r\n example/u?id=1>",
                                              "List-Unsubscribe=One-Click"))
    assert links == ["mailto:off@list.example?subject=unsubscribe", "https://list.example/u?id=1"] and one_click
    assert unsubscribe_links(_msg("<https://list.example/u>")) == (["https://list.example/u"], False)
    assert unsubscribe_links(_msg("<http://list.example/u>", "List-Unsubscribe=One-Click"))[1] is False  # needs https


def _record(store, key, sender, category="werbung", received="2026-09-20T10:00+02:00", unsub=None):
    store.record(SimpleNamespace(key=key, received=received, sender=sender, subject=key,
                                 decision=Decision(category, 0.9, {}, 0.0, 0.0), folder=None, flag=False,
                                 expires=None, unsubscribe=unsub))


def test_the_newest_mail_gives_the_links(tmp_path):
    store = Store(tmp_path / "s.db")
    _record(store, "a", "News@Shop.example", received="2026-09-20T10:00+02:00", unsub=(["https://shop/new"], True))
    _record(store, "b", "news@shop.example", received="2026-09-01T10:00+02:00", unsub=(["https://shop/old"], False))
    s = store.sender("news@shop.example")
    assert (s["unsubscribe"], s["one_click"]) == (["https://shop/new"], 1)
    store.note_unsubscribe([("news@shop.example", ["mailto:x@shop"], False, "2026-09-25T08:00+00:00")])
    assert store.sender("NEWS@shop.example")["unsubscribe"] == ["mailto:x@shop"]
    assert store.set_unsubscribed("news@shop.example", "manual") and store.sender("news@shop.example")["method"] == "manual"
    store.set_unsubscribed("news@shop.example", None)
    assert store.sender("news@shop.example")["unsubscribed"] is None
    assert not store.set_unsubscribed("nobody@x", "manual")
    store.close()


class FakeMailBox(HeaderFetch):
    def __init__(self, heads):
        self.heads = heads
        self.folder = SimpleNamespace(list=lambda: [SimpleNamespace(name="INBOX", delim=".", flags=())],
                                      set=lambda name: None)

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raw_mails(self):
        return self.heads


def test_reconcile_reads_the_links_of_logged_mails(tmp_path, monkeypatch):
    store = Store(tmp_path / "data" / "state.db")
    _record(store, "<a@x>", "news@shop.example")
    store.close()
    heads = [raw_headers("<a@x>", "News <news@shop.example>", date="Sun, 20 Sep 2026 10:00:00 +0000",
                         list_unsubscribe="<https://shop/u>", list_unsubscribe_post="List-Unsubscribe=One-Click"),
             raw_headers("<unknown@x>", "other@x", list_unsubscribe="<https://other/u>")]
    monkeypatch.setattr(reconcile, "MailBox", lambda *a, **kw: FakeMailBox(heads))
    reconcile.run_reconcile(CFG, CREDS, tmp_path, live=False)
    store = Store(tmp_path / "data" / "state.db")
    assert store.sender("news@shop.example") is None  # a dry run changes nothing
    store.close()
    reconcile.run_reconcile(CFG, CREDS, tmp_path, live=True)
    store = Store(tmp_path / "data" / "state.db")
    assert store.sender("news@shop.example")["unsubscribe"] == ["https://shop/u"]
    assert store.sender("other@x") is None  # not in the log
    store.close()


def test_senders_list(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    links = (["https://shop/u"], True)
    for i in range(3):
        _record(store, f"s{i}", "news@shop.example", unsub=links)
    _record(store, "s3", "news@shop.example", category="finanzen", received="2026-09-28T10:00+02:00")
    _record(store, "o1", "info@club.example", unsub=(["mailto:off@club"], False))
    _record(store, "n1", "friend@example.org")                                   # no link: not listed
    _record(store, "old", "gone@list.example", received="2026-01-01T10:00+01:00", unsub=links)  # too old
    store.set_unsubscribed("info@club.example", "manual")
    store.db.execute("UPDATE senders SET unsubscribed = '2026-09-10T00:00:00' WHERE address = 'info@club.example'")
    store.set_unsubscribed("gone@list.example", "one-click")  # unsubscribed, nothing since: stays listed
    store.close()
    db = queries.connect(tmp_path)
    try:
        rows = {r["address"]: r for r in queries.senders(db, now=NOW)}
        assert list(rows) == ["news@shop.example", "info@club.example", "gone@list.example"]
        shop = rows["news@shop.example"]
        assert (shop["mails"], shop["category"], shop["last"], shop["one_click"]) == (4, "werbung", "2026-09-28T10:00+02:00", 1)
        assert rows["info@club.example"]["since"] == 1 and rows["info@club.example"]["links"] == ["mailto:off@club"]
        assert rows["gone@list.example"]["mails"] == 0 and rows["gone@list.example"]["since"] == 0
        only = queries.senders(db, "finanzen", now=NOW)
        assert [(r["address"], r["mails"]) for r in only] == [("news@shop.example", 1)]
    finally:
        db.close()


def _resolve(ip):
    return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def test_one_click_posts_to_public_https_links_only(monkeypatch):
    sent = []

    def post(url, **kw):
        sent.append((url, kw["data"], kw["allow_redirects"]))
        return SimpleNamespace(status_code=200 if "ok" in url else 410)

    monkeypatch.setattr(unsubscribe.requests, "post", post)
    monkeypatch.setattr(unsubscribe.socket, "getaddrinfo", _resolve("93.184.215.14"))
    assert unsubscribe.one_click("https://list.example/ok?id=1") == 200
    assert sent == [("https://list.example/ok?id=1", {"List-Unsubscribe": "One-Click"}, False)]
    with pytest.raises(unsubscribe.UnsubscribeError, match="410"):
        unsubscribe.one_click("https://list.example/gone")
    with pytest.raises(unsubscribe.UnsubscribeError, match="https"):
        unsubscribe.one_click("http://list.example/ok")
    monkeypatch.setattr(unsubscribe.socket, "getaddrinfo", _resolve("192.168.1.1"))
    with pytest.raises(unsubscribe.UnsubscribeError, match="keine"):
        unsubscribe.one_click("https://router.example/ok")
    assert len(sent) == 2
