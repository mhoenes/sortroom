"""What the sender rules on the Rules page do, as the log shows it: each rule in words, how many of the mails of
the last SPAN_DAYS days it catches, and whether a rule above it takes everything it would, so it is never reached.

The rules are in the order they are checked, and the first one that matches wins (Config.rule_for), so a mail
counts for the first matching rule only; the mails a later rule would also match are told apart. All from the
log: no mailbox access, so it works for rules that are not saved yet, too.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta

from ..config import SenderRule, sender_address
from ..i18n import _, ngettext
from . import queries

SPAN_DAYS = 30


def describe(rule: SenderRule) -> str:
    """In words, which senders the rule matches."""
    kind, match = rule.kind, rule.match
    if kind == "address":
        return _("Exactly this address")
    if kind == "domain":
        return _("Every sender of %(domain)s", domain=match[1:])
    if kind == "subdomains":
        return _("Every sender of %(domain)s and its subdomains such as mail.%(domain)s", domain=match)
    return _('Every address that contains "%(text)s"', text=match)


def covers(above: SenderRule, rule: SenderRule) -> bool:
    """`above` matches every sender `rule` matches: below it, `rule` gets no mail at all."""
    if above.match == rule.match:
        return True
    if above.kind == "text":  # the text is part of every address of `rule`: of its own text or the part it fixes
        return above.match in rule.match
    if above.kind == "address" or rule.kind == "text":
        return False
    domain = rule.match.rpartition("@")[2]  # the domain of an address or "@domain", or the domain itself
    if above.kind == "domain":  # "@shop.de" is exactly this domain: an address of it, nothing wider
        return rule.kind == "address" and domain == above.match[1:]
    return domain == above.match or domain.endswith("." + above.match)


def recent_senders(db: sqlite3.Connection | None, now: datetime | None = None) -> Counter[str]:
    """The mails of the last SPAN_DAYS days per sender address, whoever sorted them (the model, a rule, you)."""
    counts: Counter[str] = Counter()
    if db is None:
        return counts
    since = queries._received_since(now or datetime.now(), timedelta(days=SPAN_DAYS))
    for sender, mails in db.execute(
            f"SELECT sender, COUNT(*) FROM processed WHERE sender IS NOT NULL AND {queries.RECEIVED} >= julianday(?) "
            "GROUP BY lower(sender)", (since,)):
        counts[sender_address(sender)] += mails
    return counts


def rule_notes(matches: Sequence[str], counts: Counter[str] | None = None) -> list[dict[str, str]]:
    """The note under each rule, for the matches of the rows in the order of the page; a blank row gets none.
    Each: "what" (in words), "count" (the mails it catches in the log) and "warn" (a rule above takes all its
    mails), as texts for the page. `counts` is recent_senders(); without mails in the log there is no count."""
    rules = [(row, SenderRule(m.strip().lower(), "")) for row, m in enumerate(matches) if m.strip()]
    caught: Counter[int] = Counter()  # mails whose first matching rule it is
    taken: Counter[int] = Counter()   # mails it matches too, that a rule above takes
    for address, mails in (counts or {}).items():
        first = True
        for row, rule in rules:
            if rule.matches(address):
                (caught if first else taken)[row] += mails
                first = False
    notes: list[dict[str, str]] = [{} for _row in matches]
    for place, (row, rule) in enumerate(rules):
        note = {"what": describe(rule), "count": "", "warn": ""}
        above = next((a.match for _above, a in rules[:place] if covers(a, rule)), None)
        if above:
            note["warn"] = _('Never reached: the rule "%(match)s" above already takes every mail this rule would.',
                             match=above)
        elif counts:
            parts = [ngettext("%(num)s mail in the last %(days)s days", "%(num)s mails in the last %(days)s days",
                              caught[row], days=SPAN_DAYS) if caught[row]
                     else _("No mail in the last %(days)s days", days=SPAN_DAYS)]
            if taken[row]:
                parts.append(ngettext("%(num)s more is taken by a rule above", "%(num)s more are taken by a rule above",
                                      taken[row]))
            note["count"] = " · ".join(parts)
        notes[row] = note
    return notes
