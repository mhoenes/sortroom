from pathlib import Path

import pytest
import requests

from email_sorter import jev as jev_mod
from email_sorter.config import load_config
from email_sorter.jev import Decision, JevAuthError, JevClient, JevError, build_request, parse_response
from email_sorter.mailtext import clean_body, html_to_text
from email_sorter.sorter import plan, server_folder

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config.toml")

SAMPLE_RESPONSE = {
    "id": "gen-dec-1",
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "category": {
            "type": "choice",
            "choice": "finanzen",
            "confidence": 0.91,
            "probabilities": {"finanzen": 0.93, "vertraege": 0.05, "sonstiges": 0.02},
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


# --- Jev request/response ---------------------------------------------------

def test_build_request_has_both_questions():
    req = build_request("typesafe/jev-1.13", {"subject": "x"}, CFG.descriptions)
    assert req["questions"]["category"]["type"] == "choice"
    assert set(req["questions"]["category"]["criteria"]) == set(CFG.categories)
    assert req["questions"]["needs_action"]["type"] == "noul"


def test_parse_response():
    d = parse_response(SAMPLE_RESPONSE, CFG.descriptions)
    assert (d.category, d.confidence, d.needs_action, d.cost) == ("finanzen", 0.91, 0.87, 0.00002)
    assert d.runner_up == ("vertraege", 0.05)


def test_parse_response_reads_vercel_gateway_cost():
    vercel = {
        "model": "typesafe-ai/jev",
        "answers": SAMPLE_RESPONSE["answers"],
        "usage": {"input_tokens": 275, "output_tokens": 20},
        "provider_metadata": {"gateway": {"cost": "0.00001155", "generationId": "gen_x"}},
    }
    assert parse_response(vercel, CFG.descriptions).cost == pytest.approx(0.00001155)


def test_parse_response_without_cost():
    no_cost = {"answers": SAMPLE_RESPONSE["answers"], "usage": {"input_tokens": 1}}
    assert parse_response(no_cost, CFG.descriptions).cost == 0.0


def test_shipped_config_uses_vercel():
    assert CFG.jev_endpoint == "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
    assert CFG.jev_api_key_env == "AI_GATEWAY_API_KEY"


def test_parse_response_rejects_unknown_category():
    bad = {"answers": {"category": {"choice": "nope", "confidence": 1.0}}}
    with pytest.raises(JevError):
        parse_response(bad, CFG.descriptions)


def test_parse_response_rejects_garbage():
    with pytest.raises(JevError):
        parse_response({"error": "x"}, CFG.descriptions)


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, str(body)

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


def test_client_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr(jev_mod.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(429), FakeResponse(200, SAMPLE_RESPONSE)])
    client = JevClient("key", "https://example", "m", session=session)
    assert client.decide({}, CFG.descriptions).category == "finanzen"
    assert session.calls == 2


def test_client_does_not_retry_auth_errors():
    session = FakeSession([FakeResponse(402, {"error": "no credits"})])
    client = JevClient("key", "https://example", "m", session=session)
    with pytest.raises(JevAuthError):
        client.decide({}, CFG.descriptions)
    assert session.calls == 1


# --- rules --------------------------------------------------------------------

def test_plan_moves_confident_mail():
    assert plan(decision("finanzen", 0.9), CFG) == ("INBOX/Finanzen", False, "")


def test_plan_keeps_low_confidence_mail_in_inbox():
    folder, flag, note = plan(decision("newsletter", 0.5), CFG)
    assert folder is None and not flag and "low confidence" in note


def test_plan_flags_mail_needing_action_even_if_unsure():
    folder, flag, _ = plan(decision("finanzen", 0.5, needs_action=0.95), CFG)
    assert folder is None and flag


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
