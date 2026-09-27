from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
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
    label: str = ""  # display name; defaults to the key with umlauts, e.g. "Persönlich"
    expired_folder: str | None = None  # overrides rules.expired_folder for this category


INBOX_ACTION = "inbox"  # sender rule action: leave the mail in the inbox, untouched


@dataclass(frozen=True)
class SenderRule:
    """Mail whose sender address contains `match` goes straight to `action`, without the classifier."""
    match: str   # lowercase
    action: str  # INBOX_ACTION or a category key


# Logins and the API key, set in the admin UI: mailboxes/<id>/secrets.toml ([imap] user, password)
# and secrets.toml next to config.toml ([classifier] api_key of the classification endpoint).
# Plain text, mode 0600, never backed up as .bak.
SECRETS_FILE = "secrets.toml"


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    source_folder: str
    classifier_endpoint: str
    classifier_model: str
    max_body_chars: int
    timeout_seconds: float
    min_interval_seconds: float
    min_confidence: float
    action_flag_threshold: float
    expiry_threshold: float
    expired_folder: str | None
    lookback_days: int
    min_age_hours: float
    max_per_run: int
    categories: dict[str, Category]
    sender_rules: tuple[SenderRule, ...] = ()  # checked in order, first match wins
    sort_read_at_once: bool = False  # mail already read skips the min_age_hours wait
    schedule_enabled: bool = True    # built-in schedule: a normal run every schedule_minutes
    schedule_minutes: int = 10
    reconcile_enabled: bool = True   # built-in schedule: reconcile the log with the mailbox every
    reconcile_hours: int = 24        # reconcile_hours, independent of schedule_enabled

    def rule_for(self, sender: str) -> SenderRule | None:
        """The first sender rule matching this sender address, if any (case-insensitive)."""
        sender = (sender or "").lower()
        return next((r for r in self.sender_rules if r.match in sender), None)

    @property
    def descriptions(self) -> dict[str, str]:
        return {key: cat.description for key, cat in self.categories.items()}

    def classifier_client(self, api_key: str):
        from .classifier import ClassifierClient

        return ClassifierClient(api_key, self.classifier_endpoint, self.classifier_model,
                                timeout=self.timeout_seconds, min_interval=self.min_interval_seconds)


@dataclass(frozen=True)
class Credentials:
    imap_user: str
    imap_password: str = field(repr=False)
    classifier_api_key: str = field(repr=False)


def default_label(key: str) -> str:
    """'persoenlich' -> 'Persönlich', 'verdaechtig' -> 'Verdächtig'."""
    word = key.replace("_", " ")
    for a, b in (("ae", "ä"), ("oe", "ö"), ("ue", "ü")):
        word = word.replace(a, b)
    return word[:1].upper() + word[1:]


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def config_from_raw(raw: dict, where: str) -> Config:
    """Build a Config from parsed TOML holding [classifier], [imap], [rules] and [categories.*]."""
    try:
        imap, classifier, rules = raw["imap"], raw["classifier"], raw["rules"]
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
                label=str(c.get("label") or default_label(key)),
                expired_folder=c.get("expired_folder") or None,
            )
        cfg = Config(
            imap_host=imap["host"],
            imap_port=int(imap.get("port", 993)),
            source_folder=imap.get("source_folder", "INBOX"),
            classifier_endpoint=classifier["endpoint"],
            classifier_model=classifier["model"],
            max_body_chars=int(classifier.get("max_body_chars", 3000)),
            timeout_seconds=float(classifier.get("timeout_seconds", 20)),
            min_interval_seconds=float(classifier.get("min_interval_seconds", 0.0)),
            min_confidence=float(rules["min_confidence"]),
            action_flag_threshold=float(rules["action_flag_threshold"]),
            expiry_threshold=float(rules.get("expiry_threshold", 0.7)),
            expired_folder=rules.get("expired_folder") or None,
            lookback_days=int(rules["lookback_days"]),
            min_age_hours=float(rules.get("min_age_hours", 0)),
            max_per_run=int(rules["max_per_run"]),
            categories=categories,
            sender_rules=_sender_rules(raw, categories, where),
            sort_read_at_once=bool(rules.get("sort_read_at_once", False)),
            schedule_enabled=bool(raw.get("schedule", {}).get("enabled", True)),
            schedule_minutes=int(raw.get("schedule", {}).get("interval_minutes", 10)),
            reconcile_enabled=bool(raw.get("schedule", {}).get("reconcile_enabled", True)),
            reconcile_hours=int(raw.get("schedule", {}).get("reconcile_hours", 24)),
        )
    except KeyError as e:
        raise ConfigError(f"{where}: missing setting {e}") from None

    if len(cfg.categories) < 2:
        raise ConfigError(f"{where}: at least two categories are required")
    if not 1 <= cfg.schedule_minutes <= 1440:
        raise ConfigError(f"{where}: schedule.interval_minutes must be between 1 and 1440")
    if not 1 <= cfg.reconcile_hours <= 720:
        raise ConfigError(f"{where}: schedule.reconcile_hours must be between 1 and 720")
    if cfg.min_age_hours < 0:
        raise ConfigError(f"{where}: min_age_hours must not be negative")
    for name in ("min_confidence", "action_flag_threshold", "expiry_threshold"):
        if not 0.0 <= getattr(cfg, name) <= 1.0:
            raise ConfigError(f"{where}: {name} must be between 0 and 1")
    return cfg


def _sender_rules(raw: dict, categories: dict[str, Category], where: str) -> tuple[SenderRule, ...]:
    """[[sender_rules]] entries, in order."""
    out = []
    for i, r in enumerate(raw.get("sender_rules", []), start=1):
        match = str(r.get("match", "")).strip().lower()
        action = str(r.get("action", "")).strip()
        if not match:
            raise ConfigError(f"{where}: sender rule {i} needs a match")
        if action != INBOX_ACTION and action not in categories:
            raise ConfigError(f"{where}: sender rule {i} ({match}): action must be "
                              f"{INBOX_ACTION!r} or a category, not {action!r}")
        out.append(SenderRule(match, action))
    return tuple(out)


# ---------------------------------------------------------------- mailboxes

_MAILBOX_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def mailbox_id_for(name: str, taken=()) -> str:
    """The folder name (id) for a mailbox's display name: 'mh@hoenes.de' -> 'mh-hoenes-de',
    'Büro' -> 'buero'; '-2', '-3' … when it is taken. Mirrored in web/static/mailbox-id.js."""
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9_]+", "-", s)).strip("-_")
    base = s[:40].rstrip("-_") or "mailbox"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base[:39 - len(str(n))].rstrip('-_')}-{n}"
    return candidate
# categories, rules and schedule a new mailbox starts from when there is none to copy
# standard categories for new mailboxes, one set per UI language
EXAMPLE_MAILBOXES = {lang: Path(__file__).with_name(f"example_mailbox.{lang}.toml") for lang in ("en", "de")}


@dataclass(frozen=True)
class Mailbox:
    """One mailbox: its settings plus the folder holding its log, lock and reports."""
    id: str
    name: str
    workspace: Path  # data/state.db, data/run.lock and reports/ live below this
    cfg: Config
    config_file: Path | None = None  # its mailbox.toml
    shared_secrets: Path | None = None  # the secrets.toml next to config.toml

    @property
    def lock_path(self) -> Path:
        return self.workspace / "data" / "run.lock"

    @property
    def secrets_path(self) -> Path:
        return self.workspace / SECRETS_FILE


def load_mailboxes(base_dir: Path, config_path: Path) -> dict[str, Mailbox]:
    """All configured mailboxes, by id (empty until the first one is added).

    config.toml holds what all mailboxes share ([classifier]); each mailboxes/<id>/mailbox.toml
    holds a mailbox's name, [imap], [rules], [schedule] and [categories.*].
    """
    shared = _read_toml(config_path)
    check_shared(shared, config_path)
    boxes: dict[str, Mailbox] = {}
    root = base_dir / "mailboxes"
    for file in sorted(root.glob("*/mailbox.toml")) if root.is_dir() else []:
        box_id = file.parent.name
        if not _MAILBOX_ID_RE.match(box_id):
            raise ConfigError(f"mailbox folder {box_id!r}: use lowercase letters, digits, _ or -")
        raw = _read_toml(file)
        if "classifier" in raw:
            raise ConfigError(f"{file}: [classifier] belongs in {config_path.name}, it is shared by all mailboxes")
        cfg = config_from_raw({**raw, "classifier": shared["classifier"]}, str(file))
        boxes[box_id] = Mailbox(box_id, str(raw.get("name") or box_id), file.parent, cfg, file,
                                config_path.with_name(SECRETS_FILE))
    return boxes


def check_shared(shared: dict, config_path: Path) -> None:
    """config.toml must have [classifier]."""
    if "classifier" not in shared:
        raise ConfigError(f"{config_path}: missing [classifier]")


# ---------------------------------------------------------------- credentials

def read_secrets(path: Path | None) -> dict:
    """A secrets.toml, {} when there is none. Its content never goes into an error message."""
    if path is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError:
        raise ConfigError(f"{path} is not valid TOML") from None


def _value(table, key: str) -> str:
    value = table.get(key) if isinstance(table, dict) else None
    return value if isinstance(value, str) else ""


def classifier_key(shared_secrets: Path | None) -> str:
    """The API key shared by all mailboxes, "" when none is set."""
    return _value(read_secrets(shared_secrets).get("classifier"), "api_key")


def stored_credentials(box: Mailbox) -> dict[str, str]:
    """imap_user, imap_password and classifier_api_key of a mailbox, "" for what is not set.
    Read on every call, so a change applies from the next run without a restart."""
    imap = read_secrets(box.secrets_path).get("imap")
    return {"imap_user": _value(imap, "user"), "imap_password": _value(imap, "password"),
            "classifier_api_key": classifier_key(box.shared_secrets)}


_WHERE = {"imap_user": "IMAP user (mailbox settings)", "imap_password": "IMAP password (mailbox settings)",
          "classifier_api_key": "API key (Global settings)"}


def load_credentials(box: Mailbox) -> Credentials:
    found = stored_credentials(box)
    missing = [_WHERE[k] for k, v in found.items() if not v]
    if missing:
        raise ConfigError(f"not set: {'; '.join(missing)}")
    return Credentials(**found)
