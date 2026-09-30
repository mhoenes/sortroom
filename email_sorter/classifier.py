"""Client for the classification model: any endpoint that speaks the TypeSafe API
(https://docs.typesafe.ai/api) - TypeSafe itself, OpenRouter's decisions API or a clone.

Everything that knows about the wire format lives in build_request() and parse_response().
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .expiry import WINDOWS

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}  # 529: overloaded
AUTH_STATUS = {401, 402, 403}  # bad key, no credits, forbidden - retrying won't help

NEEDS_ACTION_QUESTION = {
    "type": "noul",
    "instructions": "Does this email require the recipient to do something, such as pay, reply, confirm, sign or decide?",
    "criteria": {
        "true": "An action, payment or reply from the recipient is expected.",
        "false": "The email is informational only; nothing needs to be done.",
    },
}

HAS_EXPIRY_QUESTION = {
    "type": "noul",
    "instructions": "Does this email advertise an offer, discount, voucher, coupon, sale or deal that is limited in time?",
    "criteria": {
        "true": "The offer ends or expires: a deadline, end date, 'only today', 'last days', 'ends soon' or a countdown is mentioned.",
        "false": "No offer, or the offer has no time limit.",
    },
}

EXPIRY_WINDOW_QUESTION = {
    "type": "choice",
    "instructions": "Counting from the day the email was sent, when does the time-limited offer end?",
    "criteria": WINDOWS,
}


class ClassifierError(RuntimeError):
    pass


class ClassifierAuthError(ClassifierError):
    """Key/credit problem: abort the run instead of failing every mail."""


class ClassifierOutage(ClassifierError):
    """The endpoint failed for several mails in a row, retries included: stop classifying for this
    run instead of retrying every remaining mail for minutes. What was classified is still applied."""


MAX_FAILURES_IN_A_ROW = 3


@dataclass(frozen=True)
class Decision:
    category: str
    confidence: float
    probabilities: dict[str, float]
    needs_action: float
    cost: float
    has_expiry: float = 0.0
    expiry_window: str | None = None

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
            "has_expiry": HAS_EXPIRY_QUESTION,
            "expiry_window": EXPIRY_WINDOW_QUESTION,
        },
    }


def _cost(data: dict) -> float:
    # usage.cost where the provider reports it (OpenRouter does); otherwise the cost is unknown: 0
    return float((data.get("usage") or {}).get("cost") or 0.0)


def parse_response(data: dict, categories: dict[str, str]) -> Decision:
    try:
        answers = data["answers"]
        cat = answers["category"]
        choice = cat["choice"]
        probabilities = {k: float(v) for k, v in (cat.get("probabilities") or {}).items()}
        confidence = float(cat.get("confidence", probabilities.get(choice, 0.0)))
        needs_action = float((answers.get("needs_action") or {}).get("noul", 0.0))
        has_expiry = float((answers.get("has_expiry") or {}).get("noul", 0.0))
        expiry_window = (answers.get("expiry_window") or {}).get("choice")
        cost = _cost(data)
    except (KeyError, TypeError, ValueError) as e:
        raise ClassifierError(f"unexpected response shape ({e!r}): {str(data)[:300]}") from None
    if choice not in categories:
        raise ClassifierError(f"the model returned unknown category {choice!r}")
    if expiry_window not in WINDOWS:
        expiry_window = None
    return Decision(choice, confidence, probabilities, needs_action, cost, has_expiry, expiry_window)


MAX_BACKOFF_SECONDS = 60


def _retry_delay(response, attempt: int) -> float:
    """Honor Retry-After (seconds) if the endpoint sends it, else exponential backoff."""
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(max(float(header), 1.0), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass  # HTTP-date form; fall back to backoff
    return min(2**attempt, MAX_BACKOFF_SECONDS)


class ClassifierClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        model: str,
        timeout: float = 20,
        retries: int = 6,
        min_interval: float = 0.0,
        session: requests.Session | None = None,
    ):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._last_request = 0.0
        self.failures_in_a_row = 0
        self.outage: str | None = None  # set once the endpoint is taken as down; every later decide() fails fast
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def decide(self, state: dict, categories: dict[str, str]) -> Decision:
        if self.outage:
            raise ClassifierOutage(self.outage)
        payload = build_request(self.model, state, categories)
        last_error = ""
        for attempt in range(1, self.retries + 1):
            self._pace()
            r = None
            try:
                r = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = repr(e)
            else:
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:  # e.g. a proxy's error page: as good as no answer, retry
                        last_error = f"HTTP 200 without JSON: {r.text[:100]!r}"
                    else:
                        self.failures_in_a_row = 0
                        return parse_response(data, categories)
                elif r.status_code in AUTH_STATUS:
                    raise ClassifierAuthError(f"HTTP {r.status_code}: {r.text[:300]}")
                elif r.status_code not in RETRY_STATUS:
                    raise ClassifierError(f"HTTP {r.status_code}: {r.text[:300]}")
                else:
                    last_error = f"HTTP {r.status_code}"
            if attempt < self.retries:
                delay = _retry_delay(r, attempt)
                log.info("classifier request failed (%s), retrying in %.0fs", last_error, delay)
                time.sleep(delay)
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
            self.outage = (f"the classification endpoint failed for {self.failures_in_a_row} mails in a row "
                           f"({last_error}); the rest is left for the next run")
            log.error("%s", self.outage)
            raise ClassifierOutage(self.outage)
        raise ClassifierError(f"giving up after {self.retries} attempts: {last_error}")

    def _pace(self) -> None:
        """Keep at least min_interval seconds between requests (provider rate limits)."""
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
