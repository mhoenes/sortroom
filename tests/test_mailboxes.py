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
user_env = "{user_env}"
password_env = "{pw_env}"

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
    vals = {"name": box_id.title(), "host": "imap.example.org", "user_env": "IMAP_USER", "pw_env": "IMAP_PASSWORD"}
    (d / "mailbox.toml").write_text(MAILBOX.format(**{**vals, **kw}), encoding="utf-8")


def test_mailbox_folders_share_classifier_and_have_own_settings(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat", name="Privat", host="imap.example.com")
    _box(tmp_path, "gmail", name="Gmail", host="imap.gmail.com", user_env="GMAIL_USER", pw_env="GMAIL_PASSWORD")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == ["gmail", "privat"]
    assert boxes["gmail"].name == "Gmail" and boxes["gmail"].cfg.imap_host == "imap.gmail.com"
    assert boxes["privat"].cfg.classifier_model == boxes["gmail"].cfg.classifier_model == "example/model-1"
    assert boxes["gmail"].workspace == tmp_path / "mailboxes" / "gmail"
    assert boxes["gmail"].lock_path != boxes["privat"].lock_path


def test_mailbox_sections_in_the_shared_config_are_ignored(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED + MAILBOX.format(name="x", host="h", user_env="U", pw_env="P"),
                                          encoding="utf-8")
    assert load_mailboxes(tmp_path, tmp_path / "config.toml") == {}
    _box(tmp_path, "privat")
    assert list(load_mailboxes(tmp_path, tmp_path / "config.toml")) == ["privat"]


def test_each_mailbox_reads_its_own_login(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "gmail", user_env="GMAIL_USER", pw_env="GMAIL_PASSWORD")
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_PASSWORD", "app-pw")
    monkeypatch.setenv("CLASSIFIER_API_KEY", "k")
    creds = load_credentials(load_mailboxes(tmp_path, tmp_path / "config.toml")["gmail"].cfg)
    assert (creds.imap_user, creds.imap_password) == ("me@gmail.com", "app-pw")


def test_classifier_in_a_mailbox_file_is_rejected(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    f = tmp_path / "mailboxes" / "privat" / "mailbox.toml"
    f.write_text(f.read_text(encoding="utf-8") + SHARED, encoding="utf-8")
    with pytest.raises(ConfigError, match="shared by all mailboxes"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")


def test_bad_mailbox_folder_name(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "Privat Box")
    with pytest.raises(ConfigError, match="lowercase"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")


def test_no_mailbox_yet(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    assert load_mailboxes(tmp_path, tmp_path / "config.toml") == {}


def test_old_config_section_gets_a_clear_message(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED.replace("[classifier]", "[jev]"), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[jev\] is now called \[classifier\].*CLASSIFIER_API_KEY"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")
    (tmp_path / "config.toml").write_text(SHARED + 'api_key_env = "OPENROUTER_API_KEY"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="no api_key_env any more"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")


def test_shipped_files():
    shared = tomllib.loads((ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
    assert list(shared) == ["classifier"] and "api_key_env" not in shared["classifier"]
    de, en = (tomllib.loads(EXAMPLE_MAILBOXES[lang].read_text(encoding="utf-8")) for lang in ("de", "en"))
    for example in (de, en):
        assert {"imap", "rules", "schedule", "categories"} <= set(example) and "classifier" not in example
    # the English standard categories mirror the German ones: same rules, same switches, same descriptions
    assert de["rules"].keys() == en["rules"].keys() and len(de["categories"]) == len(en["categories"])
    for (_k, d), (_e, e) in zip(de["categories"].items(), en["categories"].items()):
        assert {k: v for k, v in d.items() if k != "folder"} == {k: v for k, v in e.items() if k != "folder"}             or d["description"].replace("finanzen", "finance") == e["description"]
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
    return load_mailboxes(tmp_path, tmp_path / "config.toml")["privat"].cfg


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
