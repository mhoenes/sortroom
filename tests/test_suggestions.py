import dataclasses
from datetime import datetime
from types import SimpleNamespace

from email_sorter.classifier import Decision
from email_sorter.config import SenderRule
from email_sorter.store import Store
from email_sorter.web import queries, suggestions
from support import example_config

CFG = example_config()


def _mail(store, key, sender, category, corrected_to=None, how="ui"):
    store.record(SimpleNamespace(key=key, received=datetime.now().astimezone().isoformat(timespec="minutes"),
                                 sender=sender, subject=key, decision=Decision(category, 0.9, {}, 0.0, 0.0),
                                 folder=f"INBOX/{category}", flag=False, expires=None, source="classifier"))
    if corrected_to:
        store.set_correction(key, category, corrected_to, how)


def _suggest(tmp_path, cfg=CFG):
    db = queries.connect(tmp_path)
    try:
        rules = suggestions.rule_suggestions(db, cfg)
        return rules, suggestions.category_hints(db, cfg, explained=rules)
    finally:
        db.close()


def test_a_sender_corrected_three_times_to_the_same_place_gets_a_rule(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i in range(3):
        _mail(store, f"n{i}", "News <news@shop.example>", "finanzen", "werbung")
    for i in range(2):                                           # two corrections only: not yet
        _mail(store, f"c{i}", "club@verein.example", "werbung", "persoenlich")
    store.close()
    rules, _hints = _suggest(tmp_path)
    assert [(s.match, s.target, s.count) for s in rules] == [("news@shop.example", "werbung", 3)]

    store = Store(tmp_path / "data" / "state.db")
    store.dismiss("rule", "news@shop.example", "werbung")       # dismissed: doesn't come back
    store.close()
    assert _suggest(tmp_path)[0] == []


def test_several_addresses_of_one_domain_get_a_domain_rule_but_freemail_never(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i, sender in enumerate(["a@shop.example", "b@shop.example", "b@shop.example"]):
        _mail(store, f"s{i}", sender, "finanzen", "werbung")
    for i, sender in enumerate(["x@gmail.com", "y@gmail.com", "z@gmail.com"]):
        _mail(store, f"g{i}", sender, "werbung", "persoenlich")
    store.close()
    rules, _hints = _suggest(tmp_path)
    assert [(s.match, s.target, s.senders) for s in rules] == [
        ("@shop.example", "werbung", ["a@shop.example", "b@shop.example"])]


def test_no_rule_when_the_sender_also_sends_mail_the_model_files_right_or_a_rule_exists(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i in range(3):
        _mail(store, f"n{i}", "shop@shop.example", "finanzen", "werbung")
    _mail(store, "invoice", "shop@shop.example", "finanzen")   # an invoice the model filed right
    store.close()
    assert _suggest(tmp_path)[0] == []

    store = Store(tmp_path / "data" / "state.db")
    store.db.execute("DELETE FROM processed WHERE message_key = 'invoice'")
    store.db.commit()
    store.close()
    assert len(_suggest(tmp_path)[0]) == 1
    ruled = dataclasses.replace(CFG, sender_rules=(SenderRule("@shop.example", "werbung"),))
    assert _suggest(tmp_path, ruled)[0] == []                     # the rule is there already


def test_a_category_with_many_corrections_is_pointed_out(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i in range(10):                                          # 3 of 10 promotions corrected away
        _mail(store, f"w{i}", f"s{i}@x.example", "werbung", "persoenlich" if i < 3 else None)
    for i in range(2):                                           # 2 corrected into finance: fewer than 3
        _mail(store, f"f{i}", f"t{i}@y.example", "werbung", "finanzen")
    store.close()
    _rules, hints = _suggest(tmp_path)
    assert [(h.category, h.filed, h.away, h.into) for h in hints] == [("werbung", 12, 5, 0), ("persoenlich", 0, 0, 3)]

    store = Store(tmp_path / "data" / "state.db")
    store.dismiss("category", "werbung", "")
    store.close()
    assert [h.category for h in _suggest(tmp_path)[1]] == ["persoenlich"]


def test_address_of_a_sender():
    assert suggestions.address("Shop News <News@Shop.Example>") == "news@shop.example"
    assert suggestions.address("plain@x.example") == "plain@x.example" and suggestions.address(None) == ""


def test_corrections_a_rule_explains_are_no_hint(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    for i in range(3):
        _mail(store, f"n{i}", "news@shop.example", "finanzen", "werbung")
    store.close()
    rules, hints = _suggest(tmp_path)
    assert len(rules) == 1 and hints == []                          # suggested: the rule explains them
    store = Store(tmp_path / "data" / "state.db")
    store.dismiss("rule", "news@shop.example", "werbung")
    store.close()
    assert [h.category for h in _suggest(tmp_path)[1]] == ["finanzen", "werbung"]  # no rule wanted: look at both
    ruled = dataclasses.replace(CFG, sender_rules=(SenderRule("news@shop.example", "werbung"),))
    assert _suggest(tmp_path, ruled) == ([], [])                     # a rule exists: nothing to say
