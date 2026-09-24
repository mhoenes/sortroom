from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Category:
    key: str
    description: str
    folder: str | None
    flag: bool
    flag_on_action: bool
    track_expiry: bool


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    source_folder: str
    jev_endpoint: str
    jev_model: str
    jev_api_key_env: str
    max_body_chars: int
    timeout_seconds: float
    min_interval_seconds: float
    min_confidence: float
    action_flag_threshold: float
    expiry_threshold: float
    expired_folder: str | None
    lookback_days: int
    min_age_hours: float
    keep_in_inbox_from: tuple[str, ...]
    max_per_run: int
    categories: dict[str, Category]
    imap_user_env: str = "IMAP_USER"          # names of the .env variables holding this
    imap_password_env: str = "IMAP_PASSWORD"  # mailbox's login, so several mailboxes can coexist

    def keeps_in_inbox(self, sender: str) -> bool:
        """True for senders that config.toml says to always leave in the inbox."""
        sender = (sender or "").lower()
        return any(pattern in sender for pattern in self.keep_in_inbox_from)

    @property
    def descriptions(self) -> dict[str, str]:
        return {key: cat.description for key, cat in self.categories.items()}

    def jev_client(self, api_key: str):
        from .jev import JevClient

        return JevClient(api_key, self.jev_endpoint, self.jev_model,
                         timeout=self.timeout_seconds, min_interval=self.min_interval_seconds)


@dataclass(frozen=True)
class Credentials:
    imap_user: str
    imap_password: str
    jev_api_key: str


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def config_from_raw(raw: dict, where: str) -> Config:
    """Build a Config from parsed TOML holding [jev], [imap], [rules] and [categories.*]."""
    try:
        imap, jev, rules = raw["imap"], raw["jev"], raw["rules"]
        categories = {}
        for key, c in raw["categories"].items():
            if not _KEY_RE.match(key):
                raise ConfigError(f"category key {key!r} must be lowercase letters, digits or _")
            if not c.get("description", "").strip():
                raise ConfigError(f"category {key!r} needs a description")
            categories[key] = Category(
                key=key,
                description=c["description"].strip(),
                folder=c.get("folder") or None,
                flag=bool(c.get("flag", False)),
                flag_on_action=bool(c.get("flag_on_action", True)),
                track_expiry=bool(c.get("track_expiry", False)),
            )
        cfg = Config(
            imap_host=imap["host"],
            imap_port=int(imap.get("port", 993)),
            source_folder=imap.get("source_folder", "INBOX"),
            jev_endpoint=jev["endpoint"],
            jev_model=jev["model"],
            jev_api_key_env=jev.get("api_key_env", "AI_GATEWAY_API_KEY"),
            max_body_chars=int(jev.get("max_body_chars", 3000)),
            timeout_seconds=float(jev.get("timeout_seconds", 20)),
            min_interval_seconds=float(jev.get("min_interval_seconds", 0.0)),
            min_confidence=float(rules["min_confidence"]),
            action_flag_threshold=float(rules["action_flag_threshold"]),
            expiry_threshold=float(rules.get("expiry_threshold", 0.7)),
            expired_folder=rules.get("expired_folder") or None,
            lookback_days=int(rules["lookback_days"]),
            min_age_hours=float(rules.get("min_age_hours", 0)),
            keep_in_inbox_from=tuple(s.strip().lower() for s in rules.get("keep_in_inbox_from", []) if s.strip()),
            max_per_run=int(rules["max_per_run"]),
            categories=categories,
            imap_user_env=imap.get("user_env", "IMAP_USER"),
            imap_password_env=imap.get("password_env", "IMAP_PASSWORD"),
        )
    except KeyError as e:
        raise ConfigError(f"{where}: missing setting {e}") from None

    if len(cfg.categories) < 2:
        raise ConfigError(f"{where}: at least two categories are required")
    if cfg.min_age_hours < 0:
        raise ConfigError(f"{where}: min_age_hours must not be negative")
    for name in ("min_confidence", "action_flag_threshold", "expiry_threshold"):
        if not 0.0 <= getattr(cfg, name) <= 1.0:
            raise ConfigError(f"{where}: {name} must be between 0 and 1")
    return cfg


def load_config(path: Path) -> Config:
    """A single-file config (global and mailbox settings in one config.toml)."""
    return config_from_raw(_read_toml(path), str(path))


# ---------------------------------------------------------------- several mailboxes

_MAILBOX_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LEGACY_ID = "default"


@dataclass(frozen=True)
class Mailbox:
    """One mailbox: its settings plus the folder holding its log, lock and reports."""
    id: str
    name: str
    workspace: Path  # data/state.db, data/run.lock and reports/ live below this
    cfg: Config

    @property
    def lock_path(self) -> Path:
        return self.workspace / "data" / "run.lock"


def load_mailboxes(base_dir: Path, config_path: Path) -> dict[str, Mailbox]:
    """All configured mailboxes, by id.

    config.toml holds what all mailboxes share ([jev]); each mailboxes/<id>/mailbox.toml holds a
    mailbox's name, [imap], [rules] and [categories.*]. Without a mailboxes/ folder, a config.toml
    that still has [imap] is the single mailbox "default", with its data in base_dir as before.
    """
    shared = _read_toml(config_path)
    if "jev" not in shared:
        raise ConfigError(f"{config_path}: missing [jev]")
    boxes: dict[str, Mailbox] = {}
    root = base_dir / "mailboxes"
    for file in sorted(root.glob("*/mailbox.toml")) if root.is_dir() else []:
        box_id = file.parent.name
        if not _MAILBOX_ID_RE.match(box_id):
            raise ConfigError(f"mailbox folder {box_id!r}: use lowercase letters, digits, _ or -")
        raw = _read_toml(file)
        if "jev" in raw:
            raise ConfigError(f"{file}: [jev] belongs in {config_path.name}, it is shared by all mailboxes")
        cfg = config_from_raw({**raw, "jev": shared["jev"]}, str(file))
        boxes[box_id] = Mailbox(box_id, str(raw.get("name") or box_id), file.parent, cfg)
    if boxes:
        return boxes
    if "imap" in shared:
        return {LEGACY_ID: Mailbox(LEGACY_ID, "Postfach", base_dir, config_from_raw(shared, str(config_path)))}
    raise ConfigError(f"no mailbox configured: add {root / '<id>' / 'mailbox.toml'}")


def load_credentials(cfg: Config) -> Credentials:
    names = (cfg.imap_user_env, cfg.imap_password_env, cfg.jev_api_key_env)
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError(f"missing in .env / environment: {', '.join(missing)}")
    return Credentials(*(os.environ[n] for n in names))
