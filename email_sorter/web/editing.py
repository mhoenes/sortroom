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

from .. import jobs
from ..config import (IMAP_AUTHS, INBOX_ACTION, SECRETS_FILE, Config, ConfigError, Mailbox, _read_toml,
                      config_from_raw, default_label, mailbox_id_for, read_secrets, secrets_writable, write_secrets)
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, _
from ..runtime import single_instance

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


def _checked_text(box: Mailbox, doc: tomlkit.TOMLDocument, shared_path: Path) -> str:
    """The new mailbox.toml, if the sorter would load it."""
    text = tomlkit.dumps(doc)
    try:
        config_from_raw({**tomllib.loads(text), "classifier": _read_toml(shared_path)["classifier"]},
                        box.config_file.name)
    except (ConfigError, tomllib.TOMLDecodeError, KeyError) as e:
        raise EditError(_("Not saved: %(e)s", e=e)) from None
    return text


def _write(path: Path, text: str) -> None:
    """Replace path with text, keeping the previous version as .bak."""
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(box: Mailbox, doc: tomlkit.TOMLDocument, shared_path: Path) -> None:
    _write(box.config_file, _checked_text(box, doc, shared_path))


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

def save_settings(box: Mailbox, shared_path: Path, form: dict, busy: bool = False) -> str:
    """Save the settings page. The mailbox folder (its id) follows the display name, so a new
    name can move the folder; returns the id afterwards. `busy`: a job or test is running."""
    doc = _doc(box)
    name = _text(form, "name", 60)
    if not name:
        raise EditError(_("The display name must not be empty."))
    doc["name"] = name
    imap = doc.setdefault("imap", tomlkit.table())
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError(_("IMAP server: please enter a host name."))
    imap["host"] = host
    imap["port"] = _number(form, "imap_port", _("Port"), 1, 65535, integer=True)
    source = _folder(form, "source_folder", _("Inbox"))
    imap["source_folder"] = source or "INBOX"
    auth, oauth = _sign_in_method(form, imap, box.cfg) if "imap_auth" in form else (box.cfg.imap_auth, {})

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
        if "reconcile_hours" in form:  # likewise
            schedule["reconcile_enabled"] = _checked(form, "reconcile_enabled")
            schedule["reconcile_hours"] = _number(form, "reconcile_hours", _("Interval in hours"), 1, 720,
                                                  integer=True)
    rules["lookback_days"] = _number(form, "lookback_days", _("Look-back in days"), 1, 365, integer=True)
    rules["max_per_run"] = _number(form, "max_per_run", _("Max. mails per run"), 1, 5000, integer=True)
    _set(rules, "expired_folder", _folder(form, "expired_folder", _("Default folder for expired mail")))
    login = _login_changes(box, form, auth)
    if (login or any(oauth.values())) and not secrets_writable(box.secrets_path):
        raise EditError(_("%(file)s is not writable, so the login cannot be stored.", file=SECRETS_FILE))
    text = _checked_text(box, doc, shared_path)
    taken = {p.name for p in box.workspace.parent.iterdir() if p.is_dir()} - {box.id}
    new_id = mailbox_id_for(name, taken)
    if new_id == box.id:
        _write(box.config_file, text)
    else:
        _move_mailbox(box, new_id, text, busy)
    secrets = box.workspace.with_name(new_id) / SECRETS_FILE  # it moved along with the folder
    if login:
        write_secrets(secrets, "imap", login)
    if oauth:
        write_secrets(secrets, "oauth", oauth)
    return new_id


def _login_changes(box: Mailbox, form: dict, auth: str) -> dict:
    """What the settings form changes in the stored login: {} for nothing. The user field shows the
    stored user (empty: none stored); the password field is write-only, empty keeps it, and it is
    only used with auth "password"."""
    if "imap_user" not in form:  # not sent: the login fields are disabled (read-only)
        return {}
    stored = _stored(box.secrets_path, "imap")
    user = _secret_text(form, "imap_user", _("User"), strip=True)
    password = _secret_text(form, "imap_password", _("Password")) if auth == "password" else ""
    changes = {} if user == stored.get("user", "") else {"user": user or None}
    if password:
        changes["password"] = password
    return changes


_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,200}$")  # Google: …apps.googleusercontent.com, Microsoft: a GUID
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{1,100}$")


def _sign_in_method(form: dict, imap: Table, before: Config | None) -> tuple[str, dict]:
    """Set [imap] auth, oauth_client_id and oauth_tenant from the form. Returns the method and the
    changes for the [oauth] secrets: a new client secret, and no refresh token any more when the
    method, app or tenant changed (that needs a new sign-in)."""
    auth = _text(form, "imap_auth", 20) or "password"
    if auth not in IMAP_AUTHS:
        raise EditError(_("Unknown sign-in method."))
    _set(imap, "auth", auth, default="password")
    if auth == "password":
        return auth, {}
    client_id = _text(form, "oauth_client_id", 200)  # Microsoft: empty = Sortroom's own app
    if (client_id or auth == "google") and not _CLIENT_ID_RE.match(client_id):
        raise EditError(_("Client ID: please copy it from your OAuth app."))
    _set(imap, "oauth_client_id", client_id, default="")
    tenant = _text(form, "oauth_tenant", 100) if auth == "microsoft" else ""
    if tenant and not _TENANT_RE.match(tenant):
        raise EditError(_("Tenant: \"common\", \"consumers\", \"organizations\" or your tenant's ID or domain."))
    _set(imap, "oauth_tenant", tenant, default="")
    oauth = {}
    secret = _secret_text(form, "oauth_client_secret", _("Client secret"), strip=True) if auth == "google" else ""
    if secret:
        oauth["client_secret"] = secret
    if before is None or (before.imap_auth, before.oauth_client_id, before.oauth_tenant) != (auth, client_id, tenant):
        oauth["refresh_token"] = None
    return auth, oauth


def _move_mailbox(box: Mailbox, new_id: str, text: str, busy: bool) -> None:
    """Rename the mailbox folder to new_id and save its settings there - both or neither."""
    target = box.workspace.with_name(new_id)
    refused = _("The mailbox is busy, so neither the name nor the folder was changed. Please try again "
                "when the run or job is done.")
    if busy:
        raise EditError(refused)
    with single_instance(box.lock_path) as acquired:  # no run may start meanwhile
        if not acquired:
            raise EditError(refused)
        try:
            os.rename(box.workspace, target)
        except OSError as e:
            raise EditError(_("The folder could not be renamed: %(e)s", e=e)) from None
        try:
            _write(target / box.config_file.name, text)
        except OSError as e:
            os.rename(target, box.workspace)
            raise EditError(_("Not saved: %(e)s", e=e)) from None
        # the lock moved along with the folder; the old path is released on leaving this block
        (target / box.lock_path.relative_to(box.workspace)).unlink(missing_ok=True)
    jobs.rename_mailbox(box.id, new_id)


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


# "Add mailbox" starts with the type of mailbox; Gmail and Outlook fix the server and the sign-in
MAILBOX_KINDS = {
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": "993", "source_folder": "INBOX", "imap_auth": "google"},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": "993", "source_folder": "INBOX",
                "imap_auth": "microsoft"},
    "imap": {"imap_auth": "password"},
}


def with_kind(form: dict) -> dict:
    """The "Add mailbox" form with the server and sign-in its mailbox type sets."""
    kind = str(form.get("kind") or "imap")
    if kind not in MAILBOX_KINDS:
        raise EditError(_("Please choose the type of mailbox."))
    return {**form, "kind": kind, **MAILBOX_KINDS[kind]}


def create_mailbox(base_dir: Path, shared_path: Path, template_file: Path, template_name: str, form: dict) -> str:
    """Write mailboxes/<id>/mailbox.toml with the rules, schedule and categories of `template_file`
    (another mailbox or the built-in example). The id comes from the display name. Returns it."""
    name = _text(form, "name", 60)
    if not name:
        raise EditError(_("The display name must not be empty."))
    root = base_dir / "mailboxes"
    box_id = mailbox_id_for(name, {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else ())
    folder = root / box_id
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError(_("IMAP server: please enter a host name."))
    user = _secret_text(form, "imap_user", _("User"), strip=True)
    imap = tomlkit.table()
    auth, oauth = _sign_in_method(form, imap, None)
    password = _secret_text(form, "imap_password", _("Password")) if auth == "password" else ""
    if auth == "password" and (not user or not password):
        raise EditError(_("Please enter the user and the password of the mailbox."))
    if not user:
        raise EditError(_("Please enter the user (the e-mail address) of the mailbox."))
    if auth == "google" and not oauth.get("client_secret"):
        raise EditError(_("Please enter the client secret of your Google OAuth app."))

    source = tomlkit.parse(template_file.read_text(encoding="utf-8"))
    doc = tomlkit.document()
    doc.add(tomlkit.comment(f'Mailbox "{name}" - created in the web UI, categories from "{template_name}"'))
    doc.add(tomlkit.nl())
    doc["name"] = name
    imap["host"] = host
    imap["port"] = _number(form, "imap_port", _("Port"), 1, 65535, integer=True)
    imap["source_folder"] = _folder(form, "source_folder", _("Inbox")) or "INBOX"
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
    write_secrets(folder / SECRETS_FILE, "imap", {"user": user, "password": password or None})
    if oauth:
        write_secrets(folder / SECRETS_FILE, "oauth", oauth)
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
    key = _secret_text(form, "api_key", _("API key"), strip=True)  # write-only: empty keeps the stored one
    secrets = shared_path.with_name(SECRETS_FILE)
    if key and not secrets_writable(secrets):
        raise EditError(_("%(file)s is not writable, so the API key cannot be stored.", file=SECRETS_FILE))

    tmp = shared_path.with_name(shared_path.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    try:
        broken_before = load_mailboxes(base_dir, shared_path).broken
        newly = {k: v for k, v in load_mailboxes(base_dir, tmp).broken.items() if broken_before.get(k) != v}
        if newly:  # a mailbox that was broken already doesn't stop the save
            raise ConfigError("; ".join(f"{k}: {v}" for k, v in newly.items()))
    except ConfigError as e:
        tmp.unlink(missing_ok=True)
        raise EditError(_("Not saved: %(e)s", e=e)) from None
    shutil.copy2(shared_path, shared_path.with_name(shared_path.name + ".bak"))
    os.replace(tmp, shared_path)
    if key:
        write_secrets(secrets, "classifier", {"api_key": key})


# ---------------------------------------------------------------- secrets (logins, API key)

_SECRET_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")


def _secret_text(form: dict, name: str, label: str, strip: bool = False) -> str:
    """A login or key field. Its value never goes into an error message."""
    value = str(form.get(name) or "")
    value = value.strip() if strip else value
    if value and not _SECRET_RE.match(value):
        raise EditError(_("%(label)s: at most 500 characters, no line breaks.", label=label))
    return value


def _stored(path: Path, section: str) -> dict:
    try:
        return dict(read_secrets(path).get(section, {}))
    except ConfigError as e:
        raise EditError(str(e)) from None
