"""Learn from corrections: suggest sender rules, and point to category descriptions that may be too vague.

Both come from the corrections table (Mails page, and mails you moved that the reconcile noticed):
- a sender rule is suggested when RULE_MIN mails of one sender were corrected to the same place within
  RULE_DAYS days and none of that sender's mails the model filed elsewhere stood - for the whole domain
  (@shop.example) when several of its addresses agree, never for freemail domains;
- a category is pointed out when many of its mails were corrected in HINT_DAYS days, away from it or
  into it.
A dismissed rule suggestion doesn't come back; a dismissed category hint stays away HINT_SNOOZE_DAYS days.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from ..config import INBOX_ACTION, Config, ConfigError, Mailbox, SenderRule
from ..i18n import _
from ..store import Store
from . import _box, queries, require_login, router
from .editing import EditError, add_sender_rule
from .editor import _flash, _form, _shared_path

RULE_MIN, RULE_DAYS = 3, 60
HINT_MIN, HINT_SHARE, HINT_DAYS, HINT_SNOOZE_DAYS = 3, 0.2, 30, 30
# a domain shared by unrelated people: never suggested as a whole
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com",
            "live.de", "msn.com", "yahoo.com", "yahoo.de", "icloud.com", "me.com", "mac.com", "aol.com", "gmx.de",
            "gmx.net", "gmx.at", "gmx.ch", "web.de", "t-online.de", "freenet.de", "posteo.de", "mailbox.org",
            "proton.me", "protonmail.com", "arcor.de", "1und1.de", "online.de", "yandex.com", "mail.ru"}


@dataclass
class RuleSuggestion:
    match: str            # an address, or "@domain"
    target: str           # a category key or INBOX_ACTION
    count: int            # corrected mails
    senders: list[str] = field(default_factory=list)


@dataclass
class CategoryHint:
    category: str
    filed: int            # mails the model filed there (30 days)
    away: int             # of those, corrected elsewhere
    into: int             # mails corrected into it


def address(sender: str | None) -> str:
    """The address of "Name <address>", in lower case."""
    s = (sender or "").strip().lower()
    if "<" in s:
        s = s[s.rfind("<") + 1:].rstrip(">").strip()
    return s


def _dismissed(db: sqlite3.Connection, kind: str, since: str | None = None) -> set[tuple[str, str]]:
    if not queries._has_table(db, "dismissed"):
        return set()
    sql, args = "SELECT subject, target FROM dismissed WHERE kind = ?", [kind]
    if since:
        sql, args = sql + " AND at >= ?", args + [since]
    return {(s, t) for s, t in db.execute(sql, args)}


def rule_suggestions(db: sqlite3.Connection | None, cfg: Config, now: datetime | None = None) -> list[RuleSuggestion]:
    if db is None or not queries._has_table(db, "corrections"):
        return []
    since = ((now or datetime.now()) - timedelta(days=RULE_DAYS)).isoformat(timespec="seconds")
    corrected: dict[str, Counter] = defaultdict(Counter)  # address -> where its mails were corrected to
    for sender, target in db.execute(
            "SELECT p.sender, c.corrected_to FROM corrections c JOIN processed p ON p.message_key = c.message_key "
            "WHERE c.at >= ?", (since,)):
        if address(sender) and (target == INBOX_ACTION or target in cfg.categories):
            corrected[address(sender)][target] += 1

    candidates: list[RuleSuggestion] = []
    by_domain: dict[str, list[str]] = defaultdict(list)
    for addr in corrected:
        by_domain[addr.rpartition("@")[2]].append(addr)
    covered: set[str] = set()
    for domain, addrs in by_domain.items():
        targets = sum((corrected[a] for a in addrs), Counter())
        if domain and domain not in FREEMAIL and len(addrs) >= 2 and len(targets) == 1 \
                and sum(targets.values()) >= RULE_MIN:
            candidates.append(RuleSuggestion("@" + domain, next(iter(targets)), sum(targets.values()), sorted(addrs)))
            covered.update(addrs)
    for addr, targets in corrected.items():
        if addr not in covered and len(targets) == 1 and sum(targets.values()) >= RULE_MIN:
            candidates.append(RuleSuggestion(addr, next(iter(targets)), sum(targets.values()), [addr]))

    dismissed = _dismissed(db, "rule")
    out = []
    for s in candidates:
        rule = cfg.rule_for(s.senders[0])
        if (s.match, s.target) in dismissed or (rule and rule.action == s.target):
            continue
        if _filed_elsewhere(db, s, since):
            continue  # the sender also sends mail the model files right: a rule would misfile it
        out.append(s)
    return sorted(out, key=lambda s: -s.count)


def _filed_elsewhere(db: sqlite3.Connection, s: RuleSuggestion, since: str) -> bool:
    """A mail of the sender(s) the model filed in another category that stood (wasn't corrected)."""
    rule = SenderRule(s.match, s.target)
    rows = db.execute(
        "SELECT p.sender FROM processed p LEFT JOIN corrections c ON c.message_key = p.message_key "
        "WHERE p.processed_at >= ? AND c.message_key IS NULL AND p.source = 'classifier' AND p.category != ? "
        "AND p.gone = 0", (since, s.target))
    return any(rule.matches(address(sender)) for (sender,) in rows)


def category_hints(db: sqlite3.Connection | None, cfg: Config, now: datetime | None = None,
                   explained: Sequence[RuleSuggestion] = ()) -> list[CategoryHint]:
    """Categories with many corrections. Those of senders that have a rule, or one is suggested for, don't
    count: the rule explains them better than a vague description."""
    if db is None or not queries._has_table(db, "corrections"):
        return []
    now = now or datetime.now()
    rates = queries.corrections(db, cfg.min_confidence, days=HINT_DAYS, now=now)
    since = (now - timedelta(days=HINT_DAYS)).isoformat(timespec="seconds")
    rules = [SenderRule(s.match, s.target) for s in explained] + list(cfg.sender_rules)
    away: Counter[str] = Counter()
    into: Counter[str] = Counter()
    for sender, category, target in db.execute(
            "SELECT p.sender, c.category, c.corrected_to FROM corrections c "
            "JOIN processed p ON p.message_key = c.message_key WHERE c.at >= ?", (since,)):
        if not any(r.matches(address(sender)) for r in rules):
            away[category] += 1
            into[target] += 1
    snoozed = {s for s, _t in _dismissed(db, "category", (now - timedelta(days=HINT_SNOOZE_DAYS)).isoformat())}
    out = []
    for key in cfg.categories:
        filed = rates.get(key, (0, 0))[0]
        if key in snoozed:
            continue
        if (away[key] >= HINT_MIN and away[key] >= HINT_SHARE * filed) or into[key] >= HINT_MIN:
            out.append(CategoryHint(key, filed, away[key], into[key]))
    return sorted(out, key=lambda h: -(h.away + h.into))


def for_overview(box: Mailbox) -> tuple[list[RuleSuggestion], list[CategoryHint]]:
    db = queries.connect(box.workspace)
    try:
        rules = rule_suggestions(db, box.cfg)
        return rules, category_hints(db, box.cfg, explained=rules)
    finally:
        if db:
            db.close()


@router.post("/ui/m/{box_id}/suggestions", dependencies=[Depends(require_login)])
async def suggestion_action(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    action, kind = str(form.get("action") or ""), str(form.get("kind") or "")
    subject, target = str(form.get("subject") or "").strip().lower(), str(form.get("target") or "")
    try:
        if action == "rule":
            if not subject or not (target == INBOX_ACTION or target in box.cfg.categories):
                raise EditError(_("Unknown action."))
            await run_in_threadpool(add_sender_rule, box, _shared_path(request), subject, target)
            _flash(request, _("Sender rule for %(match)s saved.", match=subject))
        elif action == "dismiss" and kind in ("rule", "category") and subject:
            store = Store(box.workspace / "data" / "state.db")
            try:
                store.dismiss(kind, subject, target)
            finally:
                store.close()
        else:
            raise EditError(_("Unknown action."))
    except (EditError, ConfigError) as e:
        _flash(request, str(e), "err")
    return RedirectResponse(f"/ui/m/{box.id}", status_code=303)
