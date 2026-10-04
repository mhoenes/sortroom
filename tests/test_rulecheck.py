import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from email_sorter.classifier import Decision
from email_sorter.config import SenderRule
from email_sorter.store import Store
from email_sorter.web import queries, rulecheck


def _mail(store, key, sender, days_ago=2, source="classifier"):
    store.record(SimpleNamespace(
        key=key, received=(datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="minutes"),
        sender=sender, sender_name="", subject=key, decision=Decision("werbung", 0.9, {}, 0.0, 0.0),
        folder="INBOX/Werbung", flag=False, expires=None, source=source))


def test_each_kind_of_rule_in_words():
    words = {m: rulecheck.describe(SenderRule(m, "inbox")) for m in ("a@shop.de", "@shop.de", "shop.de", "shop")}
    assert words == {"a@shop.de": "Genau diese Adresse", "@shop.de": "Jeder Absender von shop.de",
                     "shop.de": "Jeder Absender von shop.de und seiner Subdomains, etwa mail.shop.de",
                     "shop": 'Jede Adresse, die „shop“ enthält'}


@pytest.mark.parametrize("above, rule, hidden", [
    ("@shop.de", "news@shop.de", True), ("@shop.de", "@shop.de", True), ("@shop.de", "shop.de", False),
    ("@shop.de", "@mail.shop.de", False), ("@shop.de", "shop", False), ("@bank.de", "x@bank.dev", False),
    ("shop.de", "@shop.de", True), ("shop.de", "@mail.shop.de", True), ("shop.de", "mail.shop.de", True),
    ("shop.de", "x@mail.shop.de", True), ("mail.shop.de", "shop.de", False), ("shop.de", "shop", False),
    ("news@shop.de", "news@shop.de", True), ("news@shop.de", "@shop.de", False), ("news@shop.de", "shop", False),
    ("shop", "news@shop.de", True), ("shop", "@shop.de", True), ("shop", "shop.de", True), ("shop", "bigshop", True),
    ("shop", "sho", False), ("shop", "@mail.de", False), ("shop", "news@mail.de", False),
])
def test_a_rule_is_never_reached_when_the_rule_above_takes_all_it_would(above, rule, hidden):
    assert rulecheck.covers(SenderRule(above, "inbox"), SenderRule(rule, "inbox")) is hidden


def test_covers_never_claims_more_than_the_rules_match():
    """Whatever `covers` says, every address the rule below matches is matched by the rule above."""
    rules = [SenderRule(m, "inbox") for m in (
        "a@shop.de", "@shop.de", "shop.de", "mail.shop.de", "@mail.shop.de", "x@mail.shop.de", "shop", "sho", "mail",
        "@bank.de", "bank.de", "x@bank.dev", "bigshop", "b@bigshop.de", "bigshop.de")]
    addresses = ["a@shop.de", "b@shop.de", "a@mail.shop.de", "x@mail.shop.de", "a@bank.de", "x@bank.dev", "a@bank.de.x",
                 "b@bigshop.de", "a@bigshop.de", "sho@x.de", "mail@x.de", "a@x.mail.de"]
    for above, rule in itertools.product(rules, rules):
        if rulecheck.covers(above, rule):
            assert all(above.matches(a) for a in addresses if rule.matches(a)), (above.match, rule.match)


def test_the_log_counts_each_mail_for_the_first_rule_that_matches(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for n in range(3):
        _mail(store, f"<n{n}@x>", "Shop News <News@Shop.de>")  # a name before the address, any case
    _mail(store, "<r1@x>", "rechnung@shop.de", source="rule")
    _mail(store, "<o1@x>", "x@other.de", source="manual")
    _mail(store, "<old@x>", "news@shop.de", days_ago=40)  # outside the last 30 days
    store.close()
    db = queries.connect(tmp_path)
    try:
        counts = rulecheck.recent_senders(db)
    finally:
        db.close()
    assert counts == {"news@shop.de": 3, "rechnung@shop.de": 1, "x@other.de": 1}

    notes = rulecheck.rule_notes(["@shop.de", "rechnung@shop.de", "", "other", "typo@shp.de", "news@shop.de"], counts)
    assert notes[0] == {"what": "Jeder Absender von shop.de", "count": "4 Mails in den letzten 30 Tagen", "warn": ""}
    # below "@shop.de": never reached, it says which rule above takes everything
    assert notes[1]["warn"] == 'Wird nie erreicht: die Regel „@shop.de“ weiter oben fängt schon jede Mail ab, die diese Regel träfe.'
    assert notes[1]["count"] == "" and notes[5]["warn"].startswith("Wird nie erreicht")
    assert notes[2] == {}  # a blank row
    assert notes[3]["count"] == "1 Mail in den letzten 30 Tagen"
    assert notes[4]["count"] == "Keine Mail in den letzten 30 Tagen"  # a typo catches nothing
    # moved to the top, the same rule catches its own mails, and the wider rule says what it lost to it
    notes = rulecheck.rule_notes(["rechnung@shop.de", "@shop.de"], counts)
    assert notes[0]["count"] == "1 Mail in den letzten 30 Tagen" and notes[0]["warn"] == ""
    assert notes[1]["count"] == "3 Mails in den letzten 30 Tagen · 1 weitere Mail fängt schon eine Regel darüber ab"


def test_without_mails_in_the_log_there_is_no_count(tmp_path):
    assert rulecheck.recent_senders(None) == {}
    notes = rulecheck.rule_notes(["@shop.de", "shop"], rulecheck.recent_senders(None))
    assert notes[0]["what"] == "Jeder Absender von shop.de" and notes[0]["count"] == ""
    assert notes[1]["warn"] == ""  # "shop" is not under a rule that covers it
