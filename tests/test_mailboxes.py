import tomllib
from pathlib import Path

import pytest

from email_sorter import __main__ as cli
from email_sorter.config import ConfigError, LEGACY_ID, load_credentials, load_mailboxes
from email_sorter.migrate import migrate_mailbox, split_sections

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = (ROOT / "config" / "config.toml").read_text(encoding="utf-8")

SHARED = """
[jev]
endpoint = "https://openrouter.ai/api/alpha/decisions"
model = "typesafe/jev-1.13"
api_key_env = "OPENROUTER_API_KEY"
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


def test_legacy_single_file_is_mailbox_default(tmp_path):
    (tmp_path / "config.toml").write_text(SHIPPED, encoding="utf-8")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == [LEGACY_ID]
    box = boxes[LEGACY_ID]
    assert box.workspace == tmp_path and box.lock_path == tmp_path / "data" / "run.lock"
    assert "werbung" in box.cfg.categories


def test_mailbox_folders_share_jev_and_have_own_settings(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat", name="Privat", host="imap.strato.de")
    _box(tmp_path, "gmail", name="Gmail", host="imap.gmail.com", user_env="GMAIL_USER", pw_env="GMAIL_PASSWORD")
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == ["gmail", "privat"]
    assert boxes["gmail"].name == "Gmail" and boxes["gmail"].cfg.imap_host == "imap.gmail.com"
    assert boxes["privat"].cfg.jev_model == boxes["gmail"].cfg.jev_model == "typesafe/jev-1.13"
    assert boxes["gmail"].workspace == tmp_path / "mailboxes" / "gmail"
    assert boxes["gmail"].lock_path != boxes["privat"].lock_path


def test_mailbox_folders_win_over_legacy_sections(tmp_path):
    (tmp_path / "config.toml").write_text(SHIPPED, encoding="utf-8")  # still has [imap] etc.
    _box(tmp_path, "privat")
    assert list(load_mailboxes(tmp_path, tmp_path / "config.toml")) == ["privat"]


def test_each_mailbox_reads_its_own_login(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "gmail", user_env="GMAIL_USER", pw_env="GMAIL_PASSWORD")
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_PASSWORD", "app-pw")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    creds = load_credentials(load_mailboxes(tmp_path, tmp_path / "config.toml")["gmail"].cfg)
    assert (creds.imap_user, creds.imap_password) == ("me@gmail.com", "app-pw")


def test_jev_in_a_mailbox_file_is_rejected(tmp_path):
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


def test_nothing_configured(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    with pytest.raises(ConfigError, match="no mailbox configured"):
        load_mailboxes(tmp_path, tmp_path / "config.toml")


# ---------------------------------------------------------------- migration

def test_split_sections_keeps_comments_with_their_section():
    shared, mailbox = split_sections(SHIPPED)
    assert "[jev]" in shared and "[imap]" not in shared and "[categories." not in shared
    assert "[imap]" in mailbox and "[rules]" in mailbox and "[categories.werbung]" in mailbox
    assert "# Categories" in mailbox          # the explanation block above the first category
    assert "# OpenRouter (decisions API" in shared
    assert tomllib.loads(mailbox)["categories"].keys() == tomllib.loads(SHIPPED)["categories"].keys()


def test_migrate_dry_run_changes_nothing(tmp_path):
    (tmp_path / "config.toml").write_text(SHIPPED, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.db").write_bytes(b"db")
    result = migrate_mailbox(tmp_path, tmp_path / "config.toml", "privat", "Privat", live=False)
    assert result.exit_code == 0 and not (tmp_path / "mailboxes").exists()
    assert (tmp_path / "data" / "state.db").exists()


def test_migrate_live_moves_config_log_and_reports(tmp_path):
    (tmp_path / "config.toml").write_text(SHIPPED, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.db").write_bytes(b"db")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "dry-run-1.csv").write_text("x", encoding="utf-8")
    result = migrate_mailbox(tmp_path, tmp_path / "config.toml", "privat", "Privat", live=True)
    assert result.exit_code == 0
    box = tmp_path / "mailboxes" / "privat"
    assert (box / "data" / "state.db").read_bytes() == b"db" and not (tmp_path / "data" / "state.db").exists()
    assert (box / "reports" / "dry-run-1.csv").exists()
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == SHIPPED  # left alone
    boxes = load_mailboxes(tmp_path, tmp_path / "config.toml")
    assert list(boxes) == ["privat"] and boxes["privat"].name == "Privat"
    assert boxes["privat"].cfg.categories == load_mailboxes(ROOT, ROOT / "config" / "config.toml")[LEGACY_ID].cfg.categories
    # a second migration is refused
    assert migrate_mailbox(tmp_path, tmp_path / "config.toml", "zwei", None, live=True).exit_code == 2


def test_migrate_rejects_bad_id(tmp_path):
    (tmp_path / "config.toml").write_text(SHIPPED, encoding="utf-8")
    assert migrate_mailbox(tmp_path, tmp_path / "config.toml", "Mein Postfach", None, live=True).exit_code == 2


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


# ---------------------------------------------------------------- sender rules

def _cfg_with(tmp_path, extra: str):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    _box(tmp_path, "privat")
    f = tmp_path / "mailboxes" / "privat" / "mailbox.toml"
    f.write_text(f.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return load_mailboxes(tmp_path, tmp_path / "config.toml")["privat"].cfg


def test_sender_rules_in_order_then_legacy_list(tmp_path):
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


def test_legacy_keep_in_inbox_from_becomes_inbox_rules(tmp_path):
    cfg = _cfg_with(tmp_path, "")
    f = tmp_path / "mailboxes" / "privat" / "mailbox.toml"
    toml = f.read_text(encoding="utf-8")
    f.write_text(toml.replace("max_per_run = 200", 'max_per_run = 200\nkeep_in_inbox_from = ["Scanner@x.de"]'),
                 encoding="utf-8")
    cfg = load_mailboxes(tmp_path, tmp_path / "config.toml")["privat"].cfg
    assert [(r.match, r.action) for r in cfg.sender_rules] == [("scanner@x.de", "inbox")]


@pytest.mark.parametrize("rule, message", [
    ('match = "x@y.de"\naction = "gibtsnicht"', "action must be"),
    ('match = ""\naction = "inbox"', "needs a match"),
])
def test_invalid_sender_rules(tmp_path, rule, message):
    with pytest.raises(ConfigError, match=message):
        _cfg_with(tmp_path, f"\n[[sender_rules]]\n{rule}\n")


def test_sender_rules_travel_with_the_mailbox_on_migration():
    shared, mailbox = split_sections(SHIPPED)
    assert "[[sender_rules]]" in mailbox and "[[sender_rules]]" not in shared
    assert "# Sender rules" in mailbox


def test_default_config_path(tmp_path, monkeypatch):
    from email_sorter import runtime

    monkeypatch.setattr(runtime, "BASE_DIR", tmp_path)
    monkeypatch.delenv("SORTROOM_CONFIG", raising=False)
    monkeypatch.delenv("EMAIL_SORTER_CONFIG", raising=False)
    assert runtime.default_config_path() == tmp_path / "config" / "config.toml"
    (tmp_path / "config.toml").write_text("", encoding="utf-8")        # mounted the pre-0.6.1 way
    assert runtime.default_config_path() == tmp_path / "config.toml"
    monkeypatch.setenv("EMAIL_SORTER_CONFIG", "/x/old.toml")
    assert runtime.default_config_path() == Path("/x/old.toml")
    monkeypatch.setenv("SORTROOM_CONFIG", "/x/new.toml")
    assert runtime.default_config_path() == Path("/x/new.toml")
