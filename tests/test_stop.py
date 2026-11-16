"""A run cut short (the container stopped, the connection lost) has logged the mail it moved."""
import threading
import time

import pytest

from email_sorter import classifier as classifier_mod, imap, runtime, sorter
from email_sorter.classifier import ClassifierClient, ClassifierOutage, Decision
from email_sorter.sorter import Outcome
from email_sorter.store import Store
from support import example_config
from test_core import FakeResponse, FakeSession

CFG = example_config()


class Box:
    """Stands in for the mailbox: apply() only needs the helpers patched below."""
    def flag(self, *a, **kw):
        pass


def _outcomes(n, folder="INBOX/Werbung"):
    d = Decision("werbung", 1.0, {"werbung": 1.0}, 0.0, 0.0)
    return [Outcome(key=f"<m{i}@x>", uid=str(i), received="2026-01-01T10:00:00", sender="a@b.de", subject=f"s{i}",
                    decision=d, folder=folder, flag=False) for i in range(1, n + 1)]


@pytest.fixture
def server(monkeypatch):
    moved = []
    state = {"fail_on": None, "calls": 0}

    def move(mb, uids, folder):
        state["calls"] += 1
        if state["calls"] == state["fail_on"]:
            raise state.get("error", RuntimeError("connection lost"))
        moved.append((list(uids), folder))

    monkeypatch.setattr(sorter, "delimiter", lambda mb: "/")
    monkeypatch.setattr(sorter, "ensure_folder", lambda mb, f: None)
    monkeypatch.setattr(sorter, "move_uids", move)
    monkeypatch.setattr(imap, "UID_CHUNK", 2)
    state["moved"] = moved
    return state


def test_each_chunk_is_logged_as_soon_as_it_has_moved(tmp_path, server):
    server["fail_on"] = 2  # the second chunk of two mails fails
    store = Store(tmp_path / "state.db")
    failures = sorter.apply(Box(), _outcomes(5), store)
    assert server["moved"] == [(["1", "2"], "INBOX/Werbung")]
    assert failures == 3  # the mails of the failed chunk and the ones after it
    assert [store.is_processed(f"<m{i}@x>") for i in range(1, 6)] == [True, True, False, False, False]
    store.close()


def test_a_run_cut_off_between_chunks_has_logged_what_it_moved(tmp_path, server):
    server["fail_on"], server["error"] = 3, SystemExit("killed")  # not an Exception: nothing catches it
    store = Store(tmp_path / "state.db")
    with pytest.raises(SystemExit):
        sorter.apply(Box(), _outcomes(6), store)
    assert [store.is_processed(f"<m{i}@x>") for i in range(1, 7)] == [True] * 4 + [False] * 2
    assert [uids for uids, _ in server["moved"]] == [["1", "2"], ["3", "4"]]
    store.close()


def test_mails_that_stay_in_the_inbox_are_logged_too(tmp_path, server):
    store = Store(tmp_path / "state.db")
    assert sorter.apply(Box(), _outcomes(3, folder=None), store) == 0
    assert all(store.is_processed(f"<m{i}@x>") for i in range(1, 4))
    assert server["moved"] == []
    store.close()


def test_a_failing_folder_leaves_the_others_alone(tmp_path, server, monkeypatch):
    def ensure(mb, folder):
        if folder.endswith("Kaputt"):
            raise RuntimeError("cannot create")

    monkeypatch.setattr(sorter, "ensure_folder", ensure)
    outcomes = _outcomes(2, folder="INBOX/Kaputt") + [
        Outcome(**{**o.__dict__, "key": f"<n{o.uid}@x>", "uid": f"1{o.uid}"}) for o in _outcomes(2)]
    store = Store(tmp_path / "state.db")
    assert sorter.apply(Box(), outcomes, store) == 2
    assert not store.is_processed("<m1@x>") and store.is_processed("<n1@x>") and store.is_processed("<n2@x>")
    store.close()


# --- the stop signal -----------------------------------------------------------------

def test_the_classifier_asks_for_nothing_once_the_process_is_stopping():
    session = FakeSession([])
    client = ClassifierClient("key", "https://example", "m", session=session)
    runtime.request_stop()
    with pytest.raises(ClassifierOutage, match="shutting down"):
        client.decide({}, CFG.descriptions)
    assert session.calls == 0 and client.outage  # the run applies what it has and ends


def test_a_retry_wait_ends_when_the_process_stops():
    class Down(FakeSession):
        def post(self, *a, **kw):
            runtime.request_stop()  # the container is stopped while the endpoint is failing
            return super().post(*a, **kw)

    session = Down([FakeResponse(503)] * 5)
    started = time.monotonic()
    client = ClassifierClient("key", "https://example", "m", retries=5, session=session)
    with pytest.raises(ClassifierOutage, match="shutting down"):
        client.decide({}, CFG.descriptions)
    assert session.calls == 1 and time.monotonic() - started < 1.5  # not through the 2 s, 4 s, ... of backoff


def test_the_schedule_starts_nothing_while_stopping(tmp_path):
    from email_sorter.scheduler import Scheduler
    runtime.request_stop()
    assert Scheduler.tick(Scheduler.__new__(Scheduler)) == []


def test_wait_idle_waits_for_a_run_and_gives_up_after_the_timeout(tmp_path):
    lock = tmp_path / "run.lock"
    release = threading.Event()
    holding = threading.Event()

    def run():
        with runtime.single_instance(lock):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=run)
    t.start()
    assert holding.wait(2)
    assert runtime.wait_idle([lock], 0.3) is False  # still running
    threading.Timer(0.3, release.set).start()
    assert runtime.wait_idle([lock], 3) is True  # the run ended
    t.join()
    assert runtime.wait_idle([tmp_path / "none.lock"], 0.1) is True  # never ran


def test_shutting_down_waits_for_a_run_that_is_still_going(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from email_sorter import api

    box = SimpleNamespace(id="privat", lock_path=tmp_path / "run.lock")
    monkeypatch.setattr(api, "load_mailboxes", lambda *a: {"privat": box})
    release, holding = threading.Event(), threading.Event()

    def run():
        with runtime.single_instance(box.lock_path):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=run)
    t.start()
    assert holding.wait(2)
    threading.Timer(0.4, release.set).start()
    started = time.monotonic()
    api._stop_runs()
    t.join()
    assert runtime.stopping()  # the run was told to wrap up, and it was waited for
    assert 0.3 < time.monotonic() - started < 3

    monkeypatch.setattr(api, "SHUTDOWN_WAIT", 0.2)  # a run that doesn't end in time doesn't hold the shutdown up
    release.clear()
    t = threading.Thread(target=run)
    holding.clear()
    t.start()
    assert holding.wait(2)
    started = time.monotonic()
    api._stop_runs()
    assert time.monotonic() - started < 2
    release.set()
    t.join()
