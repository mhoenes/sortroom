"""Client for the Jev decision model on OpenRouter (POST /api/alpha/decisions).

The endpoint is alpha, so everything that knows about its request/response
shape lives in build_request() and parse_response().
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
AUTH_STATUS = {401, 402, 403}  # bad key, no credits, forbidden - retrying won't help

NEEDS_ACTION_QUESTION = {
    "type": "noul",
    "instructions": "Does this email require the recipient to do something, such as pay, reply, confirm, sign or decide?",
    "criteria": {
        "true": "An action, payment or reply from the recipient is expected.",
        "false": "The email is informational only; nothing needs to be done.",
    },
}


class JevError(RuntimeError):
    pass


class JevAuthError(JevError):
    """Key/credit problem: abort the run instead of failing every mail."""


@dataclass(frozen=True)
class Decision:
    category: str
    confidence: float
    probabilities: dict[str, float]
    needs_action: float
    cost: float

    @property
    def runner_up(self) -> tuple[str, float] | None:
        others = sorted(
            ((k, p) for k, p in self.probabilities.items() if k != self.category),
            key=lambda kp: kp[1],
            reverse=True,
        )
        return others[0] if others else None


def build_request(model: str, state: dict, categories: dict[str, str]) -> dict:
    return {
        "model": model,
        "state": state,
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "Which folder of a private, personal mailbox should this email be filed in?",
                "criteria": categories,
            },
            "needs_action": NEEDS_ACTION_QUESTION,
        },
    }


def parse_response(data: dict, categories: dict[str, str]) -> Decision:
    try:
        answers = data["answers"]
        cat = answers["category"]
        choice = cat["choice"]
        probabilities = {k: float(v) for k, v in (cat.get("probabilities") or {}).items()}
        confidence = float(cat.get("confidence", probabilities.get(choice, 0.0)))
        needs_action = float((answers.get("needs_action") or {}).get("noul", 0.0))
        cost = float((data.get("usage") or {}).get("cost", 0.0))
    except (KeyError, TypeError, ValueError) as e:
        raise JevError(f"unexpected response shape ({e!r}): {str(data)[:300]}") from None
    if choice not in categories:
        raise JevError(f"Jev returned unknown category {choice!r}")
    return Decision(choice, confidence, probabilities, needs_action, cost)


class JevClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        model: str,
        timeout: float = 20,
        retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "email-sorter",
            }
        )

    def decide(self, state: dict, categories: dict[str, str]) -> Decision:
        payload = build_request(self.model, state, categories)
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = repr(e)
            else:
                if r.status_code == 200:
                    return parse_response(r.json(), categories)
                if r.status_code in AUTH_STATUS:
                    raise JevAuthError(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code not in RETRY_STATUS:
                    raise JevError(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
                last_error = f"HTTP {r.status_code}"
            if attempt < self.retries:
                delay = 2**attempt
                log.info("Jev request failed (%s), retrying in %ss", last_error, delay)
                time.sleep(delay)
        raise JevError(f"giving up after {self.retries} attempts: {last_error}")
