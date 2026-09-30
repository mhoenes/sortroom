import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from imap_tools import MailMessage

from email_sorter import __main__ as cli
from email_sorter import sorter
from email_sorter.config import Config, Credentials
from email_sorter.classifier import Decision, ClassifierError
from email_sorter.store import Store
from support import example_config

ROOT = Path(__file__).resolve().parent.parent
CFG = example_config()
CREDS = Credentials("u", "p", "k")


def test_month_windows_newest_first():
    assert sorter.month_windows(date(2026, 8, 15), date(2026, 9, 24)) == [
        (date(2026, 9, 1), date(2026, 9, 24)),
        (date(2026, 8, 15), date(2026, 9, 1)),
    ]
    assert sorter.month_windows(date(2025, 12, 20), date(2026, 1, 10)) == [
        (date(2026, 1, 1), date(2026, 1, 10)),
        (date(2025, 12, 20), date(2026, 1, 1)),
    ]
    assert sorter.month_windows(date(2026, 9, 1), date(2026, 9, 1)) == []


def _mail(uid: int, subject: str) -> MailMessage:
    raw = (f"Message-ID: <m{uid}@x>\r\nFrom: Shop <news@shop.de>\r\nTo: me@x.de\r\n"
           f"Subject: {subject}\r\nDate: Mon, 21 Sep 2026 10:00:00 +0200\r\n\r\nText").encode()
    msg = MailMessage([(f"{uid} (UID {uid} RFC822 {{1}}".encode(), raw)])
    msg.__dict__["_uid"] = str(uid)
    return msg


class FakeMailBox:
    """Enough of imap_tools.MailBox for run_backfill; ignores date criteria."""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.mails = {str(u): _mail(u, f"Angebot {u}") for u in range(1, 8)}
        self.moves, self.fetches, self.bulks = [], 0, []
        self.folder = SimpleNamespace(
            list=lambda: [SimpleNamespace(name="INBOX", delim=".")],
            exists=lambda n: True, set=lambda n: None, create=lambda n: None, subscribe=lambda n, v: None,
        )
        FakeMailBox.instances.append(self)

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, criteria, **kw):
        self.fetches += 1
        self.bulks.append((kw.get("headers_only", False), kw.get("bulk")))
        text = str(criteria)
        if "UID" in text:
            wanted = text.split("UID ")[1].rstrip(")").split(",")
            return iter([self.mails[u] for u in wanted if u in self.mails])
        return iter(list(self.mails.values()))

    def move(self, uids, folder):
        self.moves.append((sorted(uids, key=int), folder))
        for u in uids:
            self.mails.pop(u)

    def flag(self, *a, **kw):
        pass


class FakeClassifier:
    def __init__(self, fail_subjects=()):
        self.calls, self.fail = 0, set(fail_subjects)

    def decide(self, state, categories):
        self.calls += 1
        if state["subject"] in self.fail:
            raise ClassifierError("boom")
        return Decision("werbung", 1.0, {"werbung": 1.0}, 0.0, 0.0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeMailBox.instances.clear()
    monkeypatch.setattr(sorter, "MailBox", FakeMailBox)
    monkeypatch.setattr(sorter, "month_windows", lambda since, until: [(since, until)])
    small = Config(**{**CFG.__dict__, "max_per_run": 3, "min_age_hours": 0})
    return SimpleNamespace(cfg=small, tmp=tmp_path, monkeypatch=monkeypatch)


def _use_classifier(env, classifier):
    env.monkeypatch.setattr(Config, "classifier_client", lambda self, key: classifier)


def test_backfill_live_processes_everything_in_batches(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    mb = FakeMailBox.instances[0]
    assert code == 0 and classifier.calls == 7
    assert [len(uids) for uids, _ in mb.moves] == [3, 3, 1]  # batches of max_per_run, newest first
    assert mb.moves[0][0] == ["5", "6", "7"]
    store = Store(env.tmp / "data" / "state.db")
    assert all(store.is_processed(f"<m{u}@x>") for u in range(1, 8))
    store.close()


def test_backfill_dry_run_terminates_and_classifies_each_mail_once(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=False, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 0 and classifier.calls == 7
    assert FakeMailBox.instances[0].moves == []
    report = next((env.tmp / "reports").glob("dry-run-*.csv"))
    assert len(report.read_text(encoding="utf-8-sig").splitlines()) == 8  # header + 7


def test_backfill_respects_limit(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=4)
    assert classifier.calls == 4
    assert sum(len(u) for u, _ in FakeMailBox.instances[0].moves) == 4


def test_backfill_skips_failed_mail_instead_of_looping(env):
    classifier = FakeClassifier(fail_subjects={"Angebot 6"})
    _use_classifier(env, classifier)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 1 and classifier.calls == 7  # failure reported, nothing classified twice
    store = Store(env.tmp / "data" / "state.db")
    assert not store.is_processed("<m6@x>")  # retried by the next backfill
    store.close()


def test_since_rejects_future_and_bad_dates():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        cli._past_date("2999-01-01")
    with pytest.raises(argparse.ArgumentTypeError):
        cli._past_date("gestern")
    assert cli._past_date("2020-01-01") == date(2020, 1, 1)


HOLD_LOCK = """
import sys, time
from pathlib import Path
from email_sorter.runtime import single_instance
with single_instance(Path(sys.argv[1])) as held:
    print("held" if held else "busy", flush=True)
    time.sleep(60)
"""


def _other_process(lock):
    """Another process that takes the lock and keeps it until it is killed."""
    proc = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(lock)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_the_lock_holds_against_another_process_and_ends_with_it(tmp_path):
    from email_sorter.runtime import is_locked, single_instance
    lock = tmp_path / ".locks" / "privat.lock"
    proc = _other_process(lock)
    try:
        assert is_locked(lock)
        with single_instance(lock) as acquired:
            assert acquired is False
    finally:
        proc.kill()  # like a crashed run: nothing is released by hand
        proc.wait()
        proc.stdout.close()
    deadline = time.monotonic() + 5  # Linux releases it at once, Windows a moment after the process ended
    while is_locked(lock) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not is_locked(lock)
    with single_instance(lock) as acquired:
        assert acquired is True


def test_unsolicited_fetch_response_is_ignored(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    orig_fetch = FakeMailBox.fetch

    def fetch_with_noise(self, criteria, **kw):
        result = list(orig_fetch(self, criteria, **kw))
        if "UID" in str(criteria):
            noise = SimpleNamespace(uid=None, subject="", headers={})
            result.insert(1, noise)
        return iter(result)

    env.monkeypatch.setattr(FakeMailBox, "fetch", fetch_with_noise)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 0 and classifier.calls == 7


def test_backfill_keeps_going_when_a_batch_comes_back_short(env):
    """Regression: a batch returning fewer mails than requested used to end the month early."""
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    orig_fetch = FakeMailBox.fetch
    calls = {"n": 0}

    def fetch_dropping_one(self, criteria, **kw):
        result = list(orig_fetch(self, criteria, **kw))
        if "UID" in str(criteria):
            calls["n"] += 1
            if calls["n"] == 1:
                result = result[1:]  # server "loses" one mail in the first batch
        return iter(result)

    env.monkeypatch.setattr(FakeMailBox, "fetch", fetch_dropping_one)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 1  # the dropped mail is reported
    assert classifier.calls == 6  # but all other 6 mails were still processed in this run


def test_mail_without_uid_is_matched_by_message_key(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    orig_fetch = FakeMailBox.fetch

    def fetch_losing_uid(self, criteria, **kw):
        result = list(orig_fetch(self, criteria, **kw))
        if "UID" in str(criteria) and result:
            m = result[0]
            result[0] = SimpleNamespace(uid=None, headers=m.headers, subject=m.subject, from_=m.from_,
                                        from_values=m.from_values, to=m.to, date=m.date, date_str=m.date_str,
                                        text=m.text, html=m.html, attachments=[])
        return iter(result)

    env.monkeypatch.setattr(FakeMailBox, "fetch", fetch_losing_uid)
    code = sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 0 and classifier.calls == 7
    assert sum(len(u) for u, _ in FakeMailBox.instances[0].moves) == 7


def test_inbox_rule_senders_are_skipped_in_normal_sorting(env):
    from email_sorter.config import SenderRule
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    cfg = Config(**{**env.cfg.__dict__, "sender_rules": (SenderRule("news@shop.de", "inbox"),)})  # all fake mails
    code = sorter.run_backfill(cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None).exit_code
    assert code == 0 and classifier.calls == 0 and FakeMailBox.instances[0].moves == []


def test_category_rule_moves_without_classifier_and_ignores_the_limit(env):
    from email_sorter.config import SenderRule
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    cfg = Config(**{**env.cfg.__dict__, "sender_rules": (SenderRule("@shop.de", "finanzen"),)})
    result = sorter.run_backfill(cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None)
    assert result.exit_code == 0 and classifier.calls == 0 and result.cost_usd == 0
    moves = FakeMailBox.instances[0].moves
    assert sum(len(u) for u, _ in moves) == 7 and {f for _, f in moves} == {"INBOX.Finanzen"}
    store = Store(env.tmp / "data" / "state.db")
    rows = store.db.execute("SELECT category, confidence, source FROM processed").fetchall()
    assert set(rows) == {("finanzen", 1.0, "rule")}
    store.close()


def test_runs_are_logged(env):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None)
    store = Store(env.tmp / "data" / "state.db")
    [entry] = store.recent_runs()
    assert entry["kind"] == "backfill" and entry["detail"] == "since 2026-01-01" and entry["live"] is True
    assert entry["classified"] == 7 and entry["moved"] == 7 and entry["categories"] == {"werbung": 7}
    assert entry["exit_code"] == 0 and entry["error"] is None
    store.close()


def test_crashed_run_is_logged_with_error(env, monkeypatch):
    classifier = FakeClassifier()
    _use_classifier(env, classifier)
    monkeypatch.setattr(sorter, "classify_new", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        sorter.run_backfill(env.cfg, CREDS, env.tmp, live=True, since=date(2026, 1, 1), limit=None)
    store = Store(env.tmp / "data" / "state.db")
    [entry] = store.recent_runs()
    assert entry["exit_code"] == 1 and entry["error"] == "boom"
    store.close()


class BrokenClassifier(FakeClassifier):
    """Fails on one mail with an error that isn't a ClassifierError (a bug, an odd mail)."""

    def decide(self, state, categories):
        if state["subject"] == "Angebot 6":
            self.calls += 1
            raise KeyError("unexpected")
        return super().decide(state, categories)


def test_one_broken_mail_does_not_stop_the_run(env):
    _use_classifier(env, BrokenClassifier())
    small = Config(**{**env.cfg.__dict__, "max_per_run": 10})
    result = sorter.run(small, CREDS, env.tmp, live=True, limit=None)
    assert result.exit_code == 1 and result.failed == 1 and result.moved == 6
    store = Store(env.tmp / "data" / "state.db")
    assert not store.is_processed("<m6@x>") and store.is_processed("<m7@x>")
    store.close()


def test_expiry_errors_keep_the_decision(env):
    class OfferClassifier(FakeClassifier):
        def decide(self, state, categories):
            self.calls += 1
            return Decision("werbung", 1.0, {"werbung": 1.0}, 0.0, 0.0, has_expiry=1.0, expiry_window="within_week")

    def boom(*a):
        raise OverflowError("date value out of range")

    _use_classifier(env, OfferClassifier())
    env.monkeypatch.setattr(sorter, "resolve_expiry", boom)
    small = Config(**{**env.cfg.__dict__, "max_per_run": 10})
    result = sorter.run(small, CREDS, env.tmp, live=True, limit=None)
    assert result.exit_code == 0 and result.moved == 7 and result.time_limited_offers == 0


def test_whole_mails_are_fetched_in_small_batches(env):
    _use_classifier(env, FakeClassifier())
    sorter.run(Config(**{**env.cfg.__dict__, "max_per_run": 200}), CREDS, env.tmp, live=True, limit=None)
    bodies = [bulk for headers_only, bulk in FakeMailBox.instances[0].bulks if not headers_only]
    assert bodies and all(bulk == sorter.BODY_CHUNK <= 20 for bulk in bodies)


def test_a_classifier_outage_applies_what_was_classified(env):
    from email_sorter.classifier import ClassifierOutage

    class DownAfterTwo(FakeClassifier):
        outage = None

        def decide(self, state, categories):
            self.calls += 1
            if self.calls > 2:
                self.outage = "the classification endpoint failed for 3 mails in a row (HTTP 503)"
                raise ClassifierOutage(self.outage)
            return Decision("werbung", 1.0, {"werbung": 1.0}, 0.0, 0.0)

    classifier = DownAfterTwo()
    _use_classifier(env, classifier)
    result = sorter.run(Config(**{**env.cfg.__dict__, "max_per_run": 10}), CREDS, env.tmp, live=True, limit=None)
    assert classifier.calls == 3 and result.moved == 2 and result.failed == 5
    assert result.exit_code == 1 and "3 mails in a row" in result.error
    store = Store(env.tmp / "data" / "state.db")
    assert store.recent_runs(1)[0]["error"] == result.error
    store.close()

    classifier = DownAfterTwo()  # a backfill stops as well, after applying its batch
    _use_classifier(env, classifier)
    result = sorter.run_backfill(env.cfg, CREDS, env.tmp / "b", live=True, since=date(2026, 1, 1), limit=None)
    assert classifier.calls == 3 and result.moved == 2 and "in a row" in result.error
