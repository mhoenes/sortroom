from pathlib import Path

import pytest
import requests

from email_sorter import classifier as classifier_mod
from email_sorter.classifier import Decision, ClassifierAuthError, ClassifierClient, ClassifierError, build_request, parse_response
from email_sorter.mailtext import clean_body, html_to_text
from email_sorter.sorter import plan, server_folder
from support import example_config

ROOT = Path(__file__).resolve().parent.parent
CFG = example_config()

SAMPLE_RESPONSE = {
    "id": "gen-dec-1",
    "model": "example/model-1.0",
    "answers": {
        "category": {
            "type": "choice",
            "choice": "finanzen",
            "confidence": 0.91,
            "probabilities": {"finanzen": 0.93, "unterlagen": 0.05, "sonstiges": 0.02},
        },
        "needs_action": {"type": "noul", "noul": 0.87},
    },
    "usage": {"input_tokens": 480, "output_tokens": 30, "cost": 0.00002},
}


def decision(category="finanzen", confidence=0.9, needs_action=0.1):
    return Decision(category, confidence, {category: confidence}, needs_action, 0.0)


# --- config -----------------------------------------------------------------

def test_shipped_config_is_valid():
    assert "finanzen" in CFG.categories
    assert CFG.categories["sicherheit"].flag is True
    assert CFG.categories["persoenlich"].folder is None


# --- classifier request/response ---------------------------------------------------

def test_build_request_has_both_questions():
    req = build_request("example/model-1", {"subject": "x"}, CFG.descriptions)
    assert req["questions"]["category"]["type"] == "choice"
    assert set(req["questions"]["category"]["criteria"]) == set(CFG.categories)
    assert req["questions"]["needs_action"]["type"] == "noul"


def test_parse_response():
    d = parse_response(SAMPLE_RESPONSE, CFG.descriptions)
    assert (d.category, d.confidence, d.needs_action, d.cost) == ("finanzen", 0.91, 0.87, 0.00002)
    assert d.runner_up == ("unterlagen", 0.05)


def test_parse_response_without_cost():
    no_cost = {"answers": SAMPLE_RESPONSE["answers"], "usage": {"input_tokens": 1}}
    assert parse_response(no_cost, CFG.descriptions).cost == 0.0


def test_shipped_config_points_at_an_https_endpoint():
    assert CFG.classifier_endpoint.startswith("https://") and CFG.classifier_model


def test_parse_response_rejects_unknown_category():
    bad = {"answers": {"category": {"choice": "nope", "confidence": 1.0}}}
    with pytest.raises(ClassifierError):
        parse_response(bad, CFG.descriptions)


def test_parse_response_rejects_garbage():
    with pytest.raises(ClassifierError):
        parse_response({"error": "x"}, CFG.descriptions)


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.text = status, body, str(body)
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.parametrize("status", [429, 529])  # rate limit, overloaded
def test_client_retries_on_rate_limit(monkeypatch, status):
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(status), FakeResponse(200, SAMPLE_RESPONSE)])
    client = ClassifierClient("key", "https://example", "m", session=session)
    assert client.decide({}, CFG.descriptions).category == "finanzen"
    assert session.calls == 2


def test_client_honors_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(classifier_mod.time, "sleep", sleeps.append)
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, SAMPLE_RESPONSE)])
    ClassifierClient("key", "https://example", "m", session=session).decide({}, CFG.descriptions)
    assert sleeps == [7.0]


def test_client_backoff_is_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(classifier_mod.time, "sleep", sleeps.append)
    session = FakeSession([FakeResponse(429)] * 8)
    client = ClassifierClient("key", "https://example", "m", retries=8, session=session)
    with pytest.raises(ClassifierError, match="giving up after 8"):
        client.decide({}, CFG.descriptions)
    assert sleeps == [2, 4, 8, 16, 32, 60, 60]


def test_client_paces_requests(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(classifier_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    session = FakeSession([FakeResponse(200, SAMPLE_RESPONSE)] * 2)
    client = ClassifierClient("key", "https://example", "m", min_interval=2.0, session=session)
    client.decide({}, CFG.descriptions)
    clock[0] += 0.5  # next mail comes 0.5s later
    client.decide({}, CFG.descriptions)
    assert sleeps == [pytest.approx(1.5)]


def test_client_does_not_retry_auth_errors():
    session = FakeSession([FakeResponse(402, {"error": "no credits"})])
    client = ClassifierClient("key", "https://example", "m", session=session)
    with pytest.raises(ClassifierAuthError):
        client.decide({}, CFG.descriptions)
    assert session.calls == 1


# --- rules --------------------------------------------------------------------

def test_plan_moves_confident_mail():
    assert plan(decision("finanzen", 0.9), CFG) == ("INBOX/Finanzen", False, "")


def test_plan_keeps_low_confidence_mail_in_inbox():
    folder, flag, note = plan(decision("werbung", 0.5), CFG)
    assert folder is None and not flag and "low confidence" in note


def test_plan_flags_mail_needing_action_even_if_unsure():
    folder, flag, _ = plan(decision("finanzen", 0.5, needs_action=0.95), CFG)
    assert folder is None and flag


def test_plan_does_not_flag_advertising_needing_action():
    assert plan(decision("werbung", 1.0, needs_action=0.95), CFG) == ("INBOX/Werbung", False, "")


def test_plan_never_flags_suspicious_mail():
    assert plan(decision("verdaechtig", 0.95, needs_action=0.97), CFG) == ("INBOX/Verdächtig", False, "")
    folder, flag, _ = plan(decision("verdaechtig", 0.5, needs_action=0.97), CFG)
    assert folder is None and not flag


def test_plan_flags_security_category_and_keeps_it():
    assert plan(decision("sicherheit", 0.9), CFG) == (None, True, "")


def test_server_folder_uses_delimiter():
    assert server_folder("INBOX/Verträge", ".") == "INBOX.Verträge"
    assert server_folder("Archiv/2026/", "/") == "Archiv/2026"


# --- text cleanup -------------------------------------------------------------

def test_html_to_text():
    html = "<html><head><style>p{}</style></head><body><p>Hallo&nbsp;Welt</p><script>x()</script>Tsch&uuml;ss</body></html>"
    text = html_to_text(html)
    assert "Hallo" in text and "Welt" in text and "Tschüss" in text
    assert "x()" not in text and "p{}" not in text


def test_clean_body_drops_quotes_and_signature():
    body = "Hi,\n\n\n\nsee below.\n> old quoted text\n-- \nMax Mustermann\n+49 123"
    assert clean_body(body, 1000) == "Hi,\n\nsee below."


def test_clean_body_truncates():
    assert len(clean_body("a" * 5000, 100)) == 100


def test_move_creates_folder_the_server_says_is_missing():
    from types import SimpleNamespace

    from email_sorter.sorter import move_uids

    class Box:
        def __init__(self, create_ok=True):
            self.moves, self.created, self.create_ok = [], [], create_ok
            self.folder = SimpleNamespace(create=self.create, subscribe=lambda n, v: None)

        def create(self, name):
            if not self.create_ok:
                raise RuntimeError("NO [CANNOT] invalid name")
            self.created.append(name)

        def move(self, uids, folder):
            if folder not in self.created:
                raise RuntimeError(f"NO [TRYCREATE] No folder {folder} (Failure)")
            self.moves.append((uids, folder))

    box = Box()
    move_uids(box, ["1"], "Werbung")
    assert box.created == ["Werbung"] and box.moves == [(["1"], "Werbung")]
    with pytest.raises(RuntimeError, match="top-level label such as 'Werbung'"):
        move_uids(Box(create_ok=False), ["1"], "INBOX/Werbung")


class NoJson(FakeResponse):
    def json(self):
        raise ValueError("Expecting value")


def test_client_retries_an_answer_without_json(monkeypatch):
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda s: None)
    session = FakeSession([NoJson(200, "<html>proxy error</html>"), FakeResponse(200, SAMPLE_RESPONSE)])
    assert ClassifierClient("key", "https://example", "m", session=session).decide({}, CFG.descriptions).category == "finanzen"


def test_client_stops_after_failing_for_several_mails_in_a_row(monkeypatch):
    monkeypatch.setattr(classifier_mod.time, "sleep", lambda s: None)
    down = [FakeResponse(503)] * 2
    session = FakeSession(down * 2 + [FakeResponse(200, SAMPLE_RESPONSE)] + down * 3)
    client = ClassifierClient("key", "https://example", "m", retries=2, session=session)
    for _ in range(2):
        with pytest.raises(ClassifierError, match="giving up"):
            client.decide({}, CFG.descriptions)
    assert client.decide({}, CFG.descriptions).category == "finanzen"  # a success starts the count again
    for _ in range(2):
        with pytest.raises(ClassifierError, match="giving up"):
            client.decide({}, CFG.descriptions)
    with pytest.raises(classifier_mod.ClassifierOutage, match="3 mails in a row"):
        client.decide({}, CFG.descriptions)
    calls = session.calls
    with pytest.raises(classifier_mod.ClassifierOutage):
        client.decide({}, CFG.descriptions)  # no more requests for this run
    assert session.calls == calls
