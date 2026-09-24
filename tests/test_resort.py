from pathlib import Path
from types import SimpleNamespace

import pytest
from imap_tools import MailMessage

from email_sorter import resort
from email_sorter.config import Config, Credentials, load_config
from email_sorter.jev import Decision
from email_sorter.store import Store

CFG = Config(**{**load_config(Path(__file__).resolve().parent.parent / "config.toml").__dict__, "min_age_hours": 0})
CREDS = Credentials("u", "p", "k")

# subject -> (category, confidence) that the fake Jev answers
ANSWERS = {
    "Flug nach Lissabon": ("reisen", 0.99),
    "Tisch bei Il Vagabondo": ("termine", 0.98),
    "Neongolf Buchung": ("termine", 0.95),
    "Weg.de Gutschein": ("finanzen", 0.30),      # uncertain -> stays
    "Neuer Login": ("sicherheit", 0.95),          # no folder -> stays
}


def _mail(uid: int, subject: str) -> MailMessage:
    raw = (f"Message-ID: <r{uid}@x>\r\nFrom: a@b.de\r\nTo: me@x.de\r\nSubject: {subject}\r\n"
           f"Date: Mon, 21 Sep 2026 10:00:00 +0200\r\n\r\nText").encode()
    return MailMessage([(f"{uid} (UID {uid} RFC822 {{1}}".encode(), raw)])


class FakeMailBox:
    def __init__(self, folders):
        self.folders = folders  # server name -> {uid: MailMessage}
        self.selected, self.moves, self.created = None, [], []
        self.folder = SimpleNamespace(
            list=lambda: [SimpleNamespace(name="INBOX", delim=".")],
            exists=lambda n: n in self.folders,
            set=self._select,
            create=lambda n: (self.created.append(n), self.folders.setdefault(n, {})),
            subscribe=lambda n, v: None,
        )

    def _select(self, name):
        self.selected = name

    def login(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def uids(self, *a, **kw):
        return list(self.folders[self.selected])

    def fetch(self, criteria, **kw):
        wanted = str(criteria).split("UID ")[1].rstrip(")").split(",")
        return iter([self.folders[self.selected][u] for u in wanted if u in self.folders[self.selected]])

    def move(self, uids, target):
        self.moves.append((sorted(uids, key=int), target))
        for u in uids:
            self.folders[target][u] = self.folders[self.selected].pop(u)


class FakeJev:
    def __init__(self):
        self.calls = 0

    def decide(self, state, categories):
        self.calls += 1
        cat, conf = ANSWERS[state["subject"]]
        return Decision(cat, conf, {cat: conf}, 0.0, 0.0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    reisen = {str(u): _mail(u, s) for u, s in enumerate(ANSWERS, start=1)}
    fake = FakeMailBox({"INBOX": {}, "INBOX.Reisen": reisen, "INBOX.Werbung.Abgelaufen": {}})
    jev = FakeJev()
    monkeypatch.setattr(resort, "MailBox", lambda *a, **kw: fake)
    monkeypatch.setattr(Config, "jev_client", lambda self, key: jev)
    return SimpleNamespace(mb=fake, jev=jev, tmp=tmp_path)


def test_live_moves_only_confident_mail_to_other_folders(env):
    result = resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Reisen", live=True, limit=None)
    assert result.exit_code == 0 and result.classified == 5 and result.moved == 2
    assert env.mb.moves == [(["2", "3"], "INBOX.Termine")]
    assert env.mb.created == ["INBOX.Termine"]
    assert sorted(env.mb.folders["INBOX.Reisen"]) == ["1", "4", "5"]  # reisen, uncertain, sicherheit stay
    store = Store(env.tmp / "data" / "state.db")
    rows = dict(store.db.execute("SELECT message_key, moved_to FROM processed"))
    assert rows == {"<r1@x>": "INBOX/Reisen", "<r2@x>": "INBOX/Termine", "<r3@x>": "INBOX/Termine",
                    "<r4@x>": "INBOX/Reisen", "<r5@x>": "INBOX/Reisen"}
    flags = [f for (f,) in store.db.execute("SELECT flagged FROM processed")]
    assert flags == [0] * 5  # re-sort never stars mail
    store.close()


def test_dry_run_moves_nothing_and_writes_report(env):
    result = resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Reisen", live=False, limit=None)
    assert result.moved == 2 and env.mb.moves == []
    report = Path(result.report).read_text(encoding="utf-8-sig")
    assert "moves from INBOX/Reisen" in report and "uncertain, stays" in report
    assert not (env.tmp / "data" / "state.db").exists() or \
        Store(env.tmp / "data" / "state.db").count_category("termine") == 0


def test_server_notation_is_accepted(env):
    result = resort.run_resort(CFG, CREDS, env.tmp, "INBOX.Reisen", live=True, limit=None)
    assert result.moved == 2 and env.mb.moves == [(["2", "3"], "INBOX.Termine")]


def test_limit_takes_newest_mails(env):
    resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Reisen", live=False, limit=2)
    assert env.jev.calls == 2  # uids 5 and 4


def test_expired_offers_folder_is_refused(env):
    result = resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Werbung/Abgelaufen", live=True, limit=None)
    assert result.exit_code == 2 and env.jev.calls == 0


def test_unknown_folder(env):
    assert resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Gibtsnicht", live=False, limit=None).exit_code == 2


def test_inbox_resort_respects_min_age(env, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    env.mb.folders["INBOX"] = dict(env.mb.folders.pop("INBOX.Reisen"))
    ages = {"1": 30, "2": 2, "3": 48, "4": 1, "5": 72}  # hours since arrival
    monkeypatch.setattr(resort, "received_times",
                        lambda mb, uids: {u: now - timedelta(hours=ages[u]) for u in uids})
    cfg = Config(**{**CFG.__dict__, "min_age_hours": 24})
    resort.run_resort(cfg, CREDS, env.tmp, "INBOX", live=False, limit=None)
    assert env.jev.calls == 3  # uids 2 and 4 are younger than 24h


def test_other_folders_ignore_min_age(env, monkeypatch):
    monkeypatch.setattr(resort, "received_times", lambda mb, uids: pytest.fail("not needed"))
    cfg = Config(**{**CFG.__dict__, "min_age_hours": 24})
    resort.run_resort(cfg, CREDS, env.tmp, "INBOX/Reisen", live=False, limit=None)
    assert env.jev.calls == 5


def test_keep_in_inbox_senders_are_not_classified_or_moved(env):
    raw = (b"Message-ID: <scan@x>\r\nFrom: FromBrotherDevice@brother.com\r\nTo: me@x.de\r\n"
           b"Subject: From_BrotherDevice\r\nDate: Mon, 21 Sep 2026 10:00:00 +0200\r\n\r\nscan")
    env.mb.folders["INBOX.Reisen"]["9"] = MailMessage([(b"9 (UID 9 RFC822 {1}", raw)])
    result = resort.run_resort(CFG, CREDS, env.tmp, "INBOX/Reisen", live=True, limit=None)
    assert env.jev.calls == 5 and result.classified == 5
    assert "9" in env.mb.folders["INBOX.Reisen"]


def test_keeps_in_inbox_matches_case_insensitive_part_of_address():
    assert CFG.keeps_in_inbox("FromBrotherDevice@brother.com")
    assert CFG.keeps_in_inbox("Scanner <frombrotherdevice@BROTHER.com>")
    assert not CFG.keeps_in_inbox("info@brother.de")
    assert not CFG.keeps_in_inbox("")
