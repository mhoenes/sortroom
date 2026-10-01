import tomllib
from pathlib import Path

import pytest

from email_sorter import __main__ as cli
from email_sorter.config import EXAMPLE_MAILBOXES, ConfigError, load_credentials, load_mailboxes

ROOT = Path(__file__).resolve().parent.parent

SHARED = """
[classifier]
endpoint = "https://example.invalid/decisions"
model = "example/model-1"
"""

MAILBOX = """
name = "{name}"

[imap]
host = "{host}"

[rules]
min_confidence = 0.7
action_flag_threshold = 0.8
lookback_days = 7
max_per_run = 200

[categories.a]
description = "A"
folder = "INBOX/A"

[categories.b]
description = "B"
"""


def _box(root: Path, box_id: str, **kw):
    d = root / "mailboxes" / box_id
    d.mkdir(parents=True)
    vals = {"name": box_id.title(), "host": "imap.example.org"}
    (d / "mailbox.toml").write_text(MAILBOX.format(**{**vals, **kw}), encoding="utf-8")


def test_mailbox_folders_share_classifier_and_have_own_settings(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat", name="Privat", host="imap.example.com")
    _box(tmp_path, "gmail", name="Gmail", host="imap.gmail.com")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == ["gmail", "privat"]
    assert boxes["gmail"].name == "Gmail" and boxes["gmail"].cfg.imap_host == "imap.gmail.com"
    assert boxes["privat"].cfg.classifier_model == boxes["gmail"].cfg.classifier_model == "example/model-1"
    assert boxes["gmail"].workspace == tmp_path / "mailboxes" / "gmail"
    assert boxes["gmail"].lock_path != boxes["privat"].lock_path


def test_mailbox_sections_in_the_shared_config_are_ignored(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED + MAILBOX.format(name="x", host="h"),
                                          encoding="utf-8")
    assert load_mailboxes(tmp_path, tmp_path / "config.toml") == {}
    _box(tmp_path, "privat")
    assert list(load_mailboxes(tmp_path, tmp_path / "config.toml")) == ["privat"]


def test_each_mailbox_reads_its_own_login(tmp_path):
    from email_sorter.web.editing import write_secrets
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    write_secrets(tmp_path / "secrets.toml", "classifier", {"api_key": "k"})
    for box_id, user in (("gmail", "me@gmail.com"), ("privat", "me@example.org")):
        _box(tmp_path, box_id)
        write_secrets(tmp_path / "mailboxes" / box_id / "secrets.toml", "imap", {"user": user, "password": box_id})
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    creds = load_credentials(boxes["gmail"])
    assert (creds.imap_user, creds.imap_password, creds.classifier_api_key) == ("me@gmail.com", "gmail", "k")
    assert load_credentials(boxes["privat"]).imap_user == "me@example.org"


def test_classifier_in_a_mailbox_file_is_rejected(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    f = tmp_path / "mailboxes" / "privat" / "mailbox.toml"
    f.write_text(f.read_text(encoding="utf-8") + SHARED, encoding="utf-8")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert "privat" not in boxes and "shared by all mailboxes" in boxes.broken["privat"]


def test_bad_mailbox_folder_name(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "Privat Box")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert boxes == {} and "lowercase" in boxes.broken["Privat Box"]


def test_a_broken_mailbox_leaves_the_others_alone(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    _box(tmp_path, "arbeit")
    (tmp_path / "mailboxes" / "arbeit" / "mailbox.toml").write_text("name = 'Arbeit'\n[imap\n", encoding="utf-8")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == ["privat"] and list(boxes.broken) == ["arbeit"]
    (tmp_path / "mailboxes" / "arbeit" / "mailbox.toml").write_text(
        (tmp_path / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8").replace(
            'host = "imap.example.org"', 'host = "imap.example.org"\nport = "abc"'), encoding="utf-8")
    assert "arbeit" in load_mailboxes(tmp_path, tmp_path / "config.toml").broken  # a ValueError, too


def test_no_mailbox_yet(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    assert load_mailboxes(tmp_path, tmp_path / "config.toml") == {}


def test_shared_config_needs_the_classifier(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED.replace("[classifier]", "[model]"), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"missing \[classifier\]"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")


def test_shipped_files():
    shared = tomllib.loads((ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
    assert list(shared) == ["classifier"] and "api_key_env" not in shared["classifier"]
    de, en = (tomllib.loads(EXAMPLE_MAILBOXES[lang].read_text(encoding="utf-8")) for lang in ("de", "en"))
    for example in (de, en):
        assert {"imap", "rules", "schedule", "categories"} <= set(example) and "classifier" not in example
    # the English standard categories mirror the German ones: same rules, same switches, same descriptions
    assert de["rules"].keys() == en["rules"].keys() and len(de["categories"]) == len(en["categories"])
    for (_k, d), (_e, e) in zip(de["categories"].items(), en["categories"].items(), strict=True):
        assert ({k: v for k, v in d.items() if k != "folder"} == {k: v for k, v in e.items() if k != "folder"}
                or d["description"].replace("finanzen", "finance") == e["description"])
        assert ("folder" in d) == ("folder" in e)
    assert "werbung" in de["categories"] and en["categories"]["promotions"]["track_expiry"]


# ---------------------------------------------------------------- CLI mailbox selection

@pytest.fixture
def two_boxes(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    _box(tmp_path, "gmail")
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "setup_logging", lambda verbose: None)
    ran = []
    monkeypatch.setattr(cli, "_run_one", lambda box, args: ran.append(box.id) or 0)
    return tmp_path, ran


def test_normal_run_covers_all_mailboxes(two_boxes):
    tmp, ran = two_boxes
    assert cli.main(["--config", str(tmp / "config.toml")]) == 0
    assert ran == ["gmail", "privat"]


def test_mailbox_flag_selects_one(two_boxes):
    tmp, ran = two_boxes
    assert cli.main(["--config", str(tmp / "config.toml"), "--mailbox", "privat", "--live"]) == 0
    assert ran == ["privat"]


def test_maintenance_needs_mailbox_when_several(two_boxes):
    tmp, ran = two_boxes
    assert cli.main(["--config", str(tmp / "config.toml"), "--resort-folder", "INBOX/Reisen"]) == 2
    assert ran == []


def test_unknown_mailbox(two_boxes):
    tmp, ran = two_boxes
    assert cli.main(["--config", str(tmp / "config.toml"), "--mailbox", "nope"]) == 2


def test_cli_without_mailboxes(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "setup_logging", lambda verbose: None)
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == 2


# ---------------------------------------------------------------- sender rules

def _cfg_with(tmp_path, extra: str):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    f = tmp_path / "mailboxes" / "privat" / "mailbox.toml"
    f.write_text(f.read_text(encoding="utf-8") + extra, encoding="utf-8")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    if "privat" in boxes.broken:
        raise ConfigError(boxes.broken["privat"])
    return boxes["privat"].cfg


def test_sender_rules_in_order(tmp_path):
    cfg = _cfg_with(tmp_path, """
[[sender_rules]]
match = "Newsletter@Shop.de"
action = "a"

[[sender_rules]]
match = "@shop.de"
action = "inbox"
""")
    assert [(r.match, r.action) for r in cfg.sender_rules] == [("newsletter@shop.de", "a"), ("@shop.de", "inbox")]
    assert cfg.rule_for("NEWSLETTER@shop.de").action == "a"   # first match wins
    assert cfg.rule_for("info@shop.de").action == "inbox"


@pytest.mark.parametrize("rule, message", [
    ('match = "x@y.de"\naction = "gibtsnicht"', "action must be"),
    ('match = ""\naction = "inbox"', "needs a match"),
])
def test_invalid_sender_rules(tmp_path, rule, message):
    with pytest.raises(ConfigError, match=message):
        _cfg_with(tmp_path, f"\n[[sender_rules]]\n{rule}\n")


def test_default_config_path(tmp_path, monkeypatch):
    from email_sorter import runtime

    monkeypatch.setattr(runtime, "BASE_DIR", tmp_path)
    monkeypatch.delenv("SORTROOM_CONFIG", raising=False)
    assert runtime.default_config_path() == tmp_path / "config" / "config.toml"
    monkeypatch.setenv("SORTROOM_CONFIG", "/x/new.toml")
    assert runtime.default_config_path() == Path("/x/new.toml")


def test_cli_runs_the_other_mailboxes_and_reports_the_broken_one(tmp_path, monkeypatch, caplog):
    from email_sorter import __main__ as cli
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    (tmp_path / "mailboxes" / "arbeit").mkdir()
    (tmp_path / "mailboxes" / "arbeit" / "mailbox.toml").write_text("[imap\n", encoding="utf-8")
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "setup_logging", lambda verbose: None)
    ran = []
    monkeypatch.setattr(cli, "_run_one", lambda box, args: ran.append(box.id) or 0)
    with caplog.at_level("ERROR", logger="email_sorter"):
        code = cli.main(["--config", str(tmp_path / "config.toml")])
    assert ran == ["privat"] and code == 2
    assert any("[arbeit] configuration error, skipped" in r.getMessage() for r in caplog.records)
    assert cli.main(["--config", str(tmp_path / "config.toml"), "--mailbox", "arbeit"]) == 2
    assert cli.main(["--config", str(tmp_path / "config.toml"), "--mailbox", "privat"]) == 0


def test_inbox_is_no_category_key(tmp_path):
    with pytest.raises(ConfigError, match="'inbox' is reserved"):
        _cfg_with(tmp_path, '\n[categories.inbox]\ndescription = "Alles"\n')
