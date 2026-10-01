from __future__ import annotations

import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

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
    """Mail from a matching sender goes straight to `action`, without the classifier."""
    match: str   # lowercase
    action: str  # INBOX_ACTION or a category key

    def matches(self, address: str) -> bool:
        """`address` in lower case. A rule is
        - an address ("news@shop.de"): exactly this sender;
        - a domain with @ ("@shop.de"): every sender of exactly this domain;
        - a domain without @ ("shop.de"): this domain and its subdomains ("mail.shop.de");
        - any other text ("newsletter"): part of the address.
        Domains are compared whole, so "@bank.de" doesn't match "x@bank.de.example" or "x@bank.dev"."""
        domain = address.rpartition("@")[2]
        if self.match.startswith("@"):
            return domain == self.match[1:]
        if "@" in self.match:
            return address == self.match
        if "." in self.match:
            return domain == self.match or domain.endswith("." + self.match)
        return self.match in address


# Logins and the API key, set in the admin UI: mailboxes/<id>/secrets.toml ([imap] user, password;
# [oauth] client_secret, refresh_token) and secrets.toml next to config.toml ([classifier] api_key of
# the classification endpoint). Plain text, mode 0600, never backed up as .bak.
SECRETS_FILE = "secrets.toml"

IMAP_AUTHS = ("password", "google", "microsoft")  # [imap] auth: password or OAuth2 (XOAUTH2)


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
    imap_auth: str = "password"      # "password", or "google" / "microsoft" for OAuth2 (see oauth.py)
    oauth_client_id: str = ""        # the user's own OAuth client (not secret); Microsoft: "" = Sortroom's
    oauth_tenant: str = ""           # Microsoft only: "common" (default), "consumers" or a tenant id
    sender_rules: tuple[SenderRule, ...] = ()  # checked in order, first match wins
    sort_read_at_once: bool = False  # mail already read skips the min_age_hours wait
    schedule_enabled: bool = True    # built-in schedule: a normal run every schedule_minutes
    schedule_minutes: int = 10
    reconcile_enabled: bool = True   # built-in schedule: reconcile the log with the mailbox every
    reconcile_hours: int = 24        # reconcile_hours, independent of schedule_enabled

    def rule_for(self, sender: str) -> SenderRule | None:
        """The first sender rule matching this sender address, if any (case-insensitive)."""
        address = (sender or "").strip().lower()
        if "<" in address:  # "Name <address>"
            address = address[address.rfind("<") + 1:].rstrip(">").strip()
        if not address:
            return None
        return next((r for r in self.sender_rules if r.matches(address)), None)

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
    oauth: object | None = field(default=None, repr=False)  # an oauth.OAuthLogin instead of the password


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
            if key == INBOX_ACTION:  # a sender rule "inbox" would be ambiguous, and so would a correction
                raise ConfigError(f"category key {key!r} is reserved: sender rules use it for "
                                  "leaving mail in the inbox")
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
            imap_auth=str(imap.get("auth", "password")),
            oauth_client_id=str(imap.get("oauth_client_id", "")).strip(),
            oauth_tenant=str(imap.get("oauth_tenant", "")).strip(),
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

    if cfg.imap_auth not in IMAP_AUTHS:
        raise ConfigError(f"{where}: imap.auth must be one of {', '.join(IMAP_AUTHS)}")
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
    workspace: Path  # data/state.db and reports/ live below this
    cfg: Config
    config_file: Path | None = None  # its mailbox.toml
    shared_secrets: Path | None = None  # the secrets.toml next to config.toml

    @property
    def lock_path(self) -> Path:
        """The run lock, in mailboxes/.locks/ rather than in the mailbox folder: a folder holding an
        open file can't be renamed or deleted on Windows, and both happen under the lock."""
        return self.workspace.parent / ".locks" / f"{self.id}.lock"

    @property
    def secrets_path(self) -> Path:
        return self.workspace / SECRETS_FILE


class Mailboxes(dict):
    """The mailboxes that load, by id; `broken` maps the folders whose mailbox.toml doesn't load to
    the reason. A broken mailbox is left out, so the others keep being sorted."""

    def __init__(self, boxes=(), broken: dict[str, str] | None = None):
        super().__init__(boxes)
        self.broken: dict[str, str] = broken or {}


def load_mailboxes(base_dir: Path, config_path: Path) -> Mailboxes:
    """All configured mailboxes, by id (empty until the first one is added).

    config.toml holds what all mailboxes share ([classifier]); each mailboxes/<id>/mailbox.toml
    holds a mailbox's name, [imap], [rules], [schedule] and [categories.*]. A problem in config.toml
    raises ConfigError; one in a mailbox.toml only leaves that mailbox out (see Mailboxes.broken).
    """
    shared = _read_toml(config_path)
    check_shared(shared, config_path)
    boxes = Mailboxes()
    root = base_dir / "mailboxes"
    for file in sorted(root.glob("*/mailbox.toml")) if root.is_dir() else []:
        box_id = file.parent.name
        try:
            boxes[box_id] = _load_mailbox(box_id, file, shared, config_path)
        except (ConfigError, ValueError, TypeError) as e:  # ValueError: e.g. port = "abc"
            boxes.broken[box_id] = str(e)
    return boxes


def _load_mailbox(box_id: str, file: Path, shared: dict, config_path: Path) -> Mailbox:
    if not _MAILBOX_ID_RE.match(box_id):
        raise ConfigError(f"mailbox folder {box_id!r}: use lowercase letters, digits, _ or -")
    raw = _read_toml(file)
    if "classifier" in raw:
        raise ConfigError(f"{file}: [classifier] belongs in {config_path.name}, it is shared by all mailboxes")
    cfg = config_from_raw({**raw, "classifier": shared["classifier"]}, str(file))
    return Mailbox(box_id, str(raw.get("name") or box_id), file.parent, cfg, file, config_path.with_name(SECRETS_FILE))


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


def secrets_writable(path: Path) -> bool:
    return os.access(path.parent, os.W_OK) and (not path.exists() or os.access(path, os.W_OK))



def write_secrets(path: Path, section: str, values: dict) -> None:
    """Set (a string) or remove (None) keys of [section] in a secrets file - atomically, mode 0600
    and without a .bak copy. The file is deleted once nothing is left in it."""
    data = {k: dict(v) for k, v in read_secrets(path).items() if isinstance(v, dict)}
    table = data.setdefault(section, {})
    for key, value in values.items():
        if value:
            table[key] = value
        else:
            table.pop(key, None)
    data = {k: v for k, v in data.items() if v}
    if not data:
        path.unlink(missing_ok=True)
        return
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Written by the Sortroom admin UI. Keep it private and include it in your backup."))
    for key, value in data.items():
        doc[key] = value
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never readable by others, not even briefly
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp, path)


def _value(table, key: str) -> str:
    value = table.get(key) if isinstance(table, dict) else None
    return value if isinstance(value, str) else ""


def classifier_key(shared_secrets: Path | None) -> str:
    """The API key shared by all mailboxes, "" when none is set."""
    return _value(read_secrets(shared_secrets).get("classifier"), "api_key")


def stored_credentials(box: Mailbox) -> dict[str, str]:
    """What is stored for a mailbox, "" for what is not set: imap_user, imap_password,
    oauth_client_secret, oauth_refresh_token and classifier_api_key.
    Read on every call, so a change applies from the next run without a restart."""
    stored = read_secrets(box.secrets_path)
    imap, oauth = stored.get("imap"), stored.get("oauth")
    return {"imap_user": _value(imap, "user"), "imap_password": _value(imap, "password"),
            "oauth_client_secret": _value(oauth, "client_secret"),
            "oauth_refresh_token": _value(oauth, "refresh_token"),
            "classifier_api_key": classifier_key(box.shared_secrets)}


def _imap_login(box: Mailbox, found: dict[str, str]) -> tuple[Credentials, list[str]]:
    """The mailbox's login (password or OAuth) and what is missing for it."""
    cfg, missing = box.cfg, []
    if not found["imap_user"]:
        missing.append("IMAP user (mailbox settings)")
    oauth = None
    if cfg.imap_auth == "password":
        if not found["imap_password"]:
            missing.append("IMAP password (mailbox settings)")
    else:
        from .oauth import PROVIDERS, OAuthLogin, client_id_for
        label = PROVIDERS[cfg.imap_auth].label
        client_id = client_id_for(cfg.imap_auth, cfg.oauth_client_id)
        if not client_id:
            missing.append(f"{label} client ID (mailbox settings)")
        if PROVIDERS[cfg.imap_auth].needs_secret and not found["oauth_client_secret"]:
            missing.append(f"{label} client secret (mailbox settings)")
        if not found["oauth_refresh_token"]:
            missing.append(f"sign-in with {label} (mailbox settings)")
        oauth = OAuthLogin(cfg.imap_auth, client_id, cfg.oauth_tenant, found["oauth_client_secret"],
                           found["oauth_refresh_token"], box.secrets_path)
    creds = Credentials(found["imap_user"], "" if oauth else found["imap_password"],
                        found["classifier_api_key"], oauth)
    return creds, missing


def imap_credentials(box: Mailbox) -> Credentials:
    """The mailbox's IMAP login only (for checks and reconciles that don't ask the model)."""
    creds, missing = _imap_login(box, stored_credentials(box))
    if missing:
        raise ConfigError(f"not set: {'; '.join(missing)}")
    return creds


def load_credentials(box: Mailbox) -> Credentials:
    found = stored_credentials(box)
    creds, missing = _imap_login(box, found)
    if not found["classifier_api_key"]:
        missing.append("API key (Global settings)")
    if missing:
        raise ConfigError(f"not set: {'; '.join(missing)}")
    return creds
