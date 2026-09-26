from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from email_sorter import sorter

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, data):
        self.data, self.calls = data, []

    def uid(self, command, uids, items):
        self.calls.append((command, uids, items))
        return "OK", self.data


def test_received_times_parses_strato_response():
    client = FakeClient([
        b'68 (UID 261848 INTERNALDATE "18-Aug-2026 15:29:18 +0200")',
        b'69 (UID 261877 INTERNALDATE " 4-Sep-2026 09:34:45 +0200")',  # space-padded day
        b'70 (INTERNALDATE "24-Sep-2026 09:55:40 +0000" UID 261879)',   # other item order
        b"* garbage",
    ])
    times = sorter.received_times(SimpleNamespace(client=client), ["261848", "261877", "261879"])
    assert times == {
        "261848": datetime(2026, 8, 18, 13, 29, 18, tzinfo=timezone.utc),
        "261877": datetime(2026, 9, 4, 7, 34, 45, tzinfo=timezone.utc),
        "261879": datetime(2026, 9, 24, 9, 55, 40, tzinfo=timezone.utc),
    }
    assert client.calls == [("FETCH", "261848,261877,261879", "(INTERNALDATE)")]


def test_old_enough_keeps_only_mail_older_than_threshold():
    times = {
        "1": NOW - timedelta(hours=30),
        "2": NOW - timedelta(hours=24),      # exactly 24h: ready
        "3": NOW - timedelta(hours=23, minutes=59),
        "4": NOW - timedelta(minutes=5),
    }
    assert sorter.old_enough(times, ["1", "2", "3", "4", "5"], 24, now=NOW) == ["1", "2", "5"]  # 5: unknown -> ready


def test_old_enough_disabled_with_zero():
    assert sorter.old_enough({"1": NOW}, ["1"], 0, now=NOW) == ["1"]


def test_seen_uids_parses_flags():
    client = FakeClient([
        b'1 (UID 10 FLAGS (\\Seen \\Flagged))',
        b'2 (UID 11 FLAGS ())',
        b'3 (FLAGS (\\Answered \\Seen) UID 12)',
    ])
    assert sorter.seen_uids(SimpleNamespace(client=client), ["10", "11", "12"]) == {"10", "12"}
    assert client.calls == [("FETCH", "10,11,12", "(FLAGS)")]


def test_read_mail_skips_the_wait_only_when_enabled(monkeypatch):
    young, old = NOW - timedelta(hours=1), NOW - timedelta(hours=30)
    monkeypatch.setattr(sorter, "received_times", lambda mb, uids: {"1": old, "2": young, "3": young})
    monkeypatch.setattr(sorter, "seen_uids", lambda mb, uids: {"2"} & set(uids))
    monkeypatch.setattr(sorter, "old_enough", lambda times, uids, h: [u for u in uids if times[u] == old])
    cfg = SimpleNamespace(sort_read_at_once=False)
    assert sorter.ready_to_sort(None, ["1", "2", "3"], cfg, 24) == ["1"]
    cfg.sort_read_at_once = True
    assert sorter.ready_to_sort(None, ["1", "2", "3"], cfg, 24) == ["1", "2"]
    assert sorter.ready_to_sort(None, ["1", "2", "3"], cfg, 0) == ["1", "2", "3"]  # no wait configured
