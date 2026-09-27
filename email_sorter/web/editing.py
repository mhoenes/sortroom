"""Write UI edits back into a mailbox's TOML file.

tomlkit keeps comments and layout. Every change is validated by loading the result exactly like the
sorter does before anything is written; the previous file is kept as <name>.bak and the new one
replaces it atomically. A run reads its settings when it starts, so edits apply from the next run.
"""
from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Table

from ..config import (INBOX_ACTION, ConfigError, Mailbox, _read_toml, config_from_raw, default_label)
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, _

_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_FOLDER_RE = re.compile(r"^[^\\%*\x00-\x1f]{1,200}$")  # IMAP list wildcards and control chars excluded


class EditError(ValueError):
    """A change the user has to fix; the file was not touched."""


def writable(box: Mailbox) -> bool:
    return bool(box.config_file) and box.config_file.exists() and os.access(box.config_file, os.W_OK)


def _doc(box: Mailbox) -> tomlkit.TOMLDocument:
    if not writable(box):
        raise EditError(_("These settings are read-only."))
    return tomlkit.parse(box.config_file.read_text(encoding="utf-8"))


def _save(box: Mailbox, doc: tomlkit.TOMLDocument, shared_path: Path) -> None:
    text = tomlkit.dumps(doc)
    try:
        config_from_raw({**tomllib.loads(text), "classifier": _read_toml(shared_path)["classifier"]},
                        box.config_file.name)
    except (ConfigError, tomllib.TOMLDecodeError, KeyError) as e:
        raise EditError(_("Not saved: %(e)s", e=e)) from None
    path = box.config_file
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- form parsing

def _text(form: dict, name: str, max_len: int = 4000) -> str:
    value = str(form.get(name, "") or "").strip()
    if len(value) > max_len:
        raise EditError(_("\"%(name)s\" is too long (max. %(n)s characters).", name=name, n=max_len))
    return value


def _folder(form: dict, name: str, label: str) -> str:
    value = _text(form, name, 200).strip("/")
    if value and not _FOLDER_RE.match(value):
        raise EditError(_("%(label)s: invalid folder name.", label=label))
    return value


def _number(form: dict, name: str, label: str, lo: float, hi: float, integer: bool = False):
    raw = _text(form, name, 20).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise EditError(_("%(label)s: please enter a number.", label=label)) from None
    if not lo <= value <= hi:
        raise EditError(_("%(label)s: allowed are values from %(lo)s to %(hi)s.", label=label, lo=f"{lo:g}", hi=f"{hi:g}"))
    if integer:
        if value != int(value):
            raise EditError(_("%(label)s: please enter a whole number.", label=label))
        return int(value)
    return round(value, 4)


def _checked(form: dict, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("1", "on", "true", "yes")


def _set(table, key: str, value, default=None) -> None:
    """Set a key, or remove it when it equals the default (keeps the file short)."""
    if value == default or value in ("", None):
        if key in table:
            del table[key]
    else:
        table[key] = value


# ---------------------------------------------------------------- categories

def save_category(box: Mailbox, shared_path: Path, key: str, form: dict, create: bool = False) -> str:
    """Create or update one category. Returns its key."""
    key = key.strip().lower()
    if not _KEY_RE.match(key):
        raise EditError(_("Key: lowercase letters, digits and _ only (max. 40)."))
    doc = _doc(box)
    cats = doc.get("categories")
    if cats is None:
        raise EditError(_("The file has no categories."))
    if create and key in cats:
        raise EditError(_("The category \"%(key)s\" already exists.", key=key))
    if not create and key not in cats:
        raise EditError(_("The category \"%(key)s\" does not exist.", key=key))

    description = _text(form, "description")
    if not description:
        raise EditError(_("The description must not be empty – the model files mails by it alone."))
    label = _text(form, "label", 60)
    folder = _folder(form, "folder", _("Target folder"))
    track = _checked(form, "track_expiry")
    expired = _folder(form, "expired_folder", _("Target folder for expired mail")) if track else ""

    table: Table = cats[key] if not create else tomlkit.table()
    table["description"] = description
    _set(table, "label", label, default=default_label(key))
    _set(table, "folder", folder)
    _set(table, "flag", _checked(form, "flag"), default=False)
    _set(table, "flag_on_action", _checked(form, "flag_on_action"), default=True)
    _set(table, "track_expiry", track, default=False)
    _set(table, "expired_folder", expired)
    if create:
        cats[key] = table
    _save(box, doc, shared_path)
    return key


def delete_category(box: Mailbox, shared_path: Path, key: str) -> None:
    doc = _doc(box)
    cats = doc.get("categories") or {}
    if key not in cats:
        raise EditError(_("The category \"%(key)s\" does not exist.", key=key))
    used = [r.get("match") for r in doc.get("sender_rules", []) if r.get("action") == key]
    if used:
        raise EditError(_("Used by sender rules (%(rules)s) – change these first.", rules=", ".join(used)))
    del cats[key]
    _save(box, doc, shared_path)


# ---------------------------------------------------------------- mailbox settings

def save_settings(box: Mailbox, shared_path: Path, form: dict) -> None:
    doc = _doc(box)
    name = _text(form, "name", 60)
    if not name:
        raise EditError(_("The display name must not be empty."))
    doc["name"] = name
    imap = doc.setdefault("imap", tomlkit.table())
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError("IMAP-Server: bitte einen Hostnamen angeben.")
    imap["host"] = host
    imap["port"] = _number(form, "imap_port", _("Port"), 1, 65535, integer=True)
    source = _folder(form, "source_folder", _("Inbox"))
    imap["source_folder"] = source or "INBOX"

    rules = doc.setdefault("rules", tomlkit.table())
    rules["min_confidence"] = _number(form, "min_confidence", _("Minimum confidence"), 0, 1)
    rules["action_flag_threshold"] = _number(form, "action_flag_threshold", _("Star for needed action from"), 0, 1)
    rules["expiry_threshold"] = _number(form, "expiry_threshold", _("Time-limited offer from"), 0, 1)
    rules["min_age_hours"] = _number(form, "min_age_hours", _("Waiting time in hours"), 0, 24 * 14)
    _set(rules, "sort_read_at_once", _checked(form, "sort_read_at_once"), default=False)

    if "schedule_minutes" in form:  # the settings page always sends it; older callers leave the schedule alone
        schedule = doc.setdefault("schedule", tomlkit.table())
        schedule["enabled"] = _checked(form, "schedule_enabled")
        schedule["interval_minutes"] = _number(form, "schedule_minutes", _("Interval in minutes"), 1, 1440, integer=True)
    rules["lookback_days"] = _number(form, "lookback_days", _("Look-back in days"), 1, 365, integer=True)
    rules["max_per_run"] = _number(form, "max_per_run", _("Max. mails per run"), 1, 5000, integer=True)
    _set(rules, "expired_folder", _folder(form, "expired_folder", _("Default folder for expired mail")))
    _save(box, doc, shared_path)


def save_sender_rules(box: Mailbox, shared_path: Path, rules: list[tuple[str, str]]) -> None:
    """Replace all sender rules (order kept)."""
    doc = _doc(box)
    categories = doc.get("categories") or {}
    clean: list[tuple[str, str]] = []
    for match, action in rules:
        match = match.strip().lower()
        if not match:
            continue
        if len(match) > 200:
            raise EditError(_("Sender \"%(match)s…\" is too long.", match=match[:40]))
        if action != INBOX_ACTION and action not in categories:
            raise EditError(_("Unknown target \"%(target)s\" for %(match)s.", target=action, match=match))
        clean.append((match, action))
    old = doc.get("sender_rules")
    if [(t.get("match"), t.get("action")) for t in old or []] == clean:
        return  # nothing changed; leave the file as it is
    # A comment block before the next section is parsed as the tail of the last rule; take it
    # off and put it back after the new last rule, so it stays in front of that section.
    trailing = _detach_trailing(old[-1]) if old else []
    unused = list(old) if old else []
    aot = AoT([])
    for match, action in clean:
        # unchanged rules keep their own comments
        same = next((t for t in unused if t.get("match") == match and t.get("action") == action), None)
        if same is not None:
            unused.remove(same)
            aot.append(same)
        else:
            t = tomlkit.table()
            t["match"] = match
            t["action"] = action
            aot.append(t)
    if clean:
        aot[-1].value.body.extend(trailing)
        doc["sender_rules"] = aot
    elif old is not None:
        idx = next(i for i, (k, _) in enumerate(doc.body) if k and k.key == "sender_rules")
        before = doc.body[idx - 1][1] if idx else None
        del doc["sender_rules"]
        if isinstance(before, Table):
            before.value.body.extend(trailing)
    _save(box, doc, shared_path)


def _detach_trailing(table: Table) -> list:
    body = table.value.body
    end = len(body)
    while end and body[end - 1][0] is None:
        end -= 1
    tail = body[end:]
    del body[end:]
    return tail


def add_sender_rule(box: Mailbox, shared_path: Path, match: str, action: str) -> None:
    """Add a rule, or change the target of an existing rule for the same sender."""
    match = match.strip().lower()
    rules = [(r.match, r.action) for r in box.cfg.sender_rules]
    if any(m == match for m, _ in rules):
        rules = [(m, action if m == match else a) for m, a in rules]
    else:
        rules.append((match, action))
    save_sender_rules(box, shared_path, rules)


# ---------------------------------------------------------------- renames (maintenance)

def _renamed_path(path: str | None, old: str, new: str) -> str | None:
    if not path:
        return None
    p, o = path.strip("/"), old.strip("/")
    if p == o:
        return new.strip("/")
    if p.startswith(o + "/"):
        return new.strip("/") + p[len(o):]
    return None


def rename_folder_refs(box: Mailbox, shared_path: Path, old: str, new: str) -> int:
    """Point category folders and expired folders below `old` to `new`. Returns how many changed."""
    doc = _doc(box)
    changed = 0
    tables = [doc.get("rules") or {}] + list((doc.get("categories") or {}).values())
    for table in tables:
        for key in ("folder", "expired_folder"):
            renamed = _renamed_path(table.get(key), old, new)
            if renamed:
                table[key] = renamed
                changed += 1
    if changed:
        _save(box, doc, shared_path)
    return changed


def rename_category_key(box: Mailbox, shared_path: Path, old: str, new: str) -> None:
    """Rename a category key in the settings, including sender rules pointing to it."""
    if not _KEY_RE.match(new):
        raise EditError(_("New key: lowercase letters, digits and _ only (max. 40)."))
    doc = _doc(box)
    cats = doc.get("categories") or {}
    if old not in cats:
        raise EditError(_("The category \"%(key)s\" does not exist.", key=old))
    if new in cats:
        raise EditError(_("The category \"%(key)s\" already exists.", key=new))
    table = cats[old]
    if "label" not in table:  # keep the name shown in the UI
        table["label"] = default_label(old)
    renamed = tomlkit.table(is_super_table=True)
    for key, t in cats.items():
        renamed[new if key == old else key] = t
    doc["categories"] = renamed
    for rule in doc.get("sender_rules") or []:
        if rule.get("action") == old:
            rule["action"] = new
    _save(box, doc, shared_path)


# ---------------------------------------------------------------- new mailbox

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def can_add_mailbox(base_dir: Path) -> str | None:
    """None if a mailbox can be added here, else the reason why not."""
    root = base_dir / "mailboxes"
    if not os.access(root if root.exists() else base_dir, os.W_OK):
        return _("The folder %(folder)s/ is not writable.", folder=root.name)
    return None


def is_gmail(host: str) -> bool:
    return host.lower().rstrip(".").endswith(("gmail.com", "googlemail.com"))


def _top_level_labels(doc) -> None:
    """Gmail has no folders below the inbox: 'INBOX/Werbung' becomes the label 'Werbung'
    (clients such as Outlook still show it under the inbox)."""
    tables = [doc["rules"]] + list(doc["categories"].values())
    for table in tables:
        for key in ("folder", "expired_folder"):
            value = table.get(key)
            if value and "/" in value and value.split("/", 1)[0].upper() == "INBOX":
                table[key] = value.split("/", 1)[1]


def create_mailbox(base_dir: Path, shared_path: Path, template_file: Path, template_name: str, form: dict) -> str:
    """Write mailboxes/<id>/mailbox.toml with the rules, schedule and categories of `template_file`
    (another mailbox or the built-in example). Returns the id."""
    box_id = _text(form, "id", 40).lower()
    if not _ID_RE.match(box_id):
        raise EditError(_("Short name: lowercase letters, digits, _ or -, starting with a letter or digit."))
    folder = base_dir / "mailboxes" / box_id
    if folder.exists():
        raise EditError(_("The mailbox \"%(id)s\" already exists.", id=box_id))
    name = _text(form, "name", 60) or box_id
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError("IMAP-Server: bitte einen Hostnamen angeben.")
    envs = {}
    for field, label in (("user_env", _("Variable for the user")), ("password_env", _("Variable for the password"))):
        value = _text(form, field, 64).upper()
        if not _ENV_RE.match(value):
            raise EditError(_("%(label)s: uppercase letters, digits and _ only.", label=label))
        envs[field] = value
    if envs["user_env"] == envs["password_env"]:
        raise EditError(_("User and password need two different variables."))

    source = tomlkit.parse(template_file.read_text(encoding="utf-8"))
    doc = tomlkit.document()
    doc.add(tomlkit.comment(f'Mailbox "{name}" - created in the web UI, categories from "{template_name}"'))
    doc.add(tomlkit.nl())
    doc["name"] = name
    imap = tomlkit.table()
    imap["host"] = host
    imap["port"] = _number(form, "imap_port", "Port", 1, 65535, integer=True)
    imap["source_folder"] = _folder(form, "source_folder", _("Inbox")) or "INBOX"
    imap["user_env"] = envs["user_env"]
    imap["password_env"] = envs["password_env"]
    doc["imap"] = imap
    doc["rules"] = source["rules"]
    if "schedule" in source:
        doc["schedule"] = source["schedule"]
    doc["categories"] = source["categories"]
    if is_gmail(host):
        _top_level_labels(doc)
    text = tomlkit.dumps(doc)
    try:
        config_from_raw({**tomllib.loads(text), "classifier": _read_toml(shared_path)["classifier"]}, "mailbox.toml")
    except (ConfigError, KeyError) as e:
        raise EditError(_("Not created: %(e)s", e=e)) from None
    folder.mkdir(parents=True)
    (folder / "mailbox.toml").write_text(text, encoding="utf-8")
    return box_id


# ---------------------------------------------------------------- shared settings (config.toml)

def shared_writable(shared_path: Path) -> bool:
    return shared_path.exists() and os.access(shared_path, os.W_OK) and os.access(shared_path.parent, os.W_OK)


def save_shared(base_dir: Path, shared_path: Path, form: dict) -> None:
    """Update [classifier] in config.toml; every mailbox must still load with it."""
    from ..config import load_mailboxes

    if not shared_writable(shared_path):
        raise EditError(_("%(file)s is read-only.", file=shared_path.name))
    doc = tomlkit.parse(shared_path.read_text(encoding="utf-8"))
    classifier = doc["classifier"]
    for field, label in (("endpoint", _("Endpoint")), ("model", _("Model"))):
        value = _text(form, field, 300)
        if not value or " " in value:
            raise EditError(_("%(label)s: please fill in.", label=label))
        classifier[field] = value
    if not _text(form, "endpoint", 300).startswith("https://"):
        raise EditError(_("Endpoint: please enter an https address."))
    classifier["max_body_chars"] = _number(form, "max_body_chars", _("Mail text length"), 200, 20000, integer=True)
    classifier["timeout_seconds"] = _number(form, "timeout_seconds", _("Timeout in seconds"), 1, 300)
    classifier["min_interval_seconds"] = _number(form, "min_interval_seconds", _("Minimum interval in seconds"), 0, 60)
    language = _text(form, "language", 10) or DEFAULT_LANGUAGE
    if language not in LANGUAGES:
        raise EditError(_("Unknown language."))
    if "ui" not in doc:
        doc["ui"] = tomlkit.table()
    doc["ui"]["language"] = language

    tmp = shared_path.with_name(shared_path.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    try:
        load_mailboxes(base_dir, tmp)
    except ConfigError as e:
        tmp.unlink(missing_ok=True)
        raise EditError(_("Not saved: %(e)s", e=e)) from None
    shutil.copy2(shared_path, shared_path.with_name(shared_path.name + ".bak"))
    os.replace(tmp, shared_path)
