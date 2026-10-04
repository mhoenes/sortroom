"""Write UI edits back into a mailbox's TOML file.

tomlkit keeps comments and layout. Every change is validated by loading the result exactly like the
sorter does before anything is written; the previous file is kept as <name>.bak and the new one
replaces it atomically. A run reads its settings when it starts, so edits apply from the next run.
"""
from __future__ import annotations

import dataclasses
import functools
import os
import re
import shutil
import threading
import tomllib
import unicodedata
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Table

from .. import jobs
from ..config import (DELETE_MAX_DAYS, IMAP_AUTHS, INBOX_ACTION, SECRETS_FILE, Config, ConfigError, Credentials,
                      Mailbox, _imap_login, _read_toml, config_from_raw, default_label, mailbox_id_for, read_secrets,
                      secrets_writable, stored_credentials, valid_mailbox_id, write_secrets)
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, THEMES, _
from ..runtime import single_instance


def reserved_key_text() -> str:
    """Why "inbox" can't be a category key, in the UI language."""
    return _("\"inbox\" is reserved: sender rules use it for leaving mail in the inbox. Please choose "
             "another key.")


_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_FOLDER_RE = re.compile(r"^[^\\%*\x00-\x1f]{1,200}$")  # IMAP list wildcards and control chars excluded


class EditError(ValueError):
    """A change the user has to fix; the file was not touched. `field`: the form field it is about, if any,
    so the page can mark it."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


# Every change of a mailbox.toml or config.toml reads the file, changes it and writes it back; two at a
# time (two browser tabs, a rename job and a save) would each write back what they read. Only this
# process edits these files, so a lock of the process is enough.
_edit_lock = threading.RLock()


def _one_at_a_time(fn):
    @functools.wraps(fn)
    def locked(*args, **kwargs):
        with _edit_lock:
            return fn(*args, **kwargs)
    return locked


def writable(box: Mailbox) -> bool:
    file = box.config_file
    return file is not None and file.exists() and os.access(file, os.W_OK)


def _config_file(box: Mailbox) -> Path:
    """The mailbox.toml the UI may change."""
    if box.config_file is None or not writable(box):
        raise EditError(_("These settings are read-only."))
    return box.config_file


def _doc(box: Mailbox) -> tomlkit.TOMLDocument:
    return tomlkit.parse(_config_file(box).read_text(encoding="utf-8"))


def _checked_text(box: Mailbox, doc: tomlkit.TOMLDocument, shared_path: Path) -> str:
    """The new mailbox.toml, if the sorter would load it."""
    text = tomlkit.dumps(doc)
    try:
        config_from_raw({**tomllib.loads(text), "classifier": _read_toml(shared_path)["classifier"]},
                        _config_file(box).name)
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
    _write(_config_file(box), _checked_text(box, doc, shared_path))


# ---------------------------------------------------------------- form parsing

def _text(form: dict, name: str, max_len: int = 4000) -> str:
    value = str(form.get(name, "") or "").strip()
    if len(value) > max_len:
        raise EditError(_("\"%(name)s\" is too long (max. %(n)s characters).", name=name, n=max_len), name)
    return value


def _folder(form: dict, name: str, label: str) -> str:
    value = _text(form, name, 200).strip("/")
    if value and not _FOLDER_RE.match(value):
        raise EditError(_("%(label)s: invalid folder name.", label=label), name)
    return value


def _number(form: dict, name: str, label: str, lo: float, hi: float, integer: bool = False):
    raw = _text(form, name, 20).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise EditError(_("%(label)s: please enter a number.", label=label), name) from None
    if not lo <= value <= hi:
        raise EditError(_("%(label)s: allowed are values from %(lo)s to %(hi)s.", label=label, lo=f"{lo:g}",
                          hi=f"{hi:g}"), name)
    if integer:
        if value != int(value):
            raise EditError(_("%(label)s: please enter a whole number.", label=label), name)
        return int(value)
    return round(value, 4)


def _checked(form: dict, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("1", "on", "true", "yes")


STARS = ("never", "action", "always")


def star_choice(form: dict) -> tuple[bool, bool]:
    """(flag, flag_on_action) of a category form: one choice "star" (never, when action is needed, always), or
    the two checkboxes of the settings file's names."""
    star = str(form.get("star") or "")
    if star in STARS:
        return star == "always", star != "never"
    return _checked(form, "flag"), _checked(form, "flag_on_action")


def category_view(form: dict) -> dict:
    """A posted category form as the page shows it again (after an error, back from a test)."""
    flag, on_action = star_choice(form)
    return {**{k: str(form.get(k) or "") for k in ("key", "label", "description", "folder", "expired_folder")},
            "flag": flag, "flag_on_action": on_action, "track_expiry": _checked(form, "track_expiry")}


def key_from_label(label: str) -> str:
    """A key for a new category from its name: "Vereine & Clubs" -> "vereine_clubs" (as categories.html does)."""
    s = label.strip().lower()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(umlaut, plain)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40].strip("_")


def _set(table, key: str, value, default=None) -> None:
    """Set a key, or remove it when it equals the default (keeps the file short)."""
    if value == default or value in ("", None):
        if key in table:
            del table[key]
    else:
        table[key] = value


# ---------------------------------------------------------------- categories

@_one_at_a_time
def save_category(box: Mailbox, shared_path: Path, key: str, form: dict, create: bool = False) -> str:
    """Create or update one category. Returns its key."""
    key = key.strip().lower()
    if not _KEY_RE.match(key):
        raise EditError(_("Key: lowercase letters, digits and _ only (max. 40)."))
    doc = _doc(box)
    cats = doc.get("categories")
    if cats is None:
        raise EditError(_("The file has no categories."))
    if create and key == INBOX_ACTION:
        raise EditError(reserved_key_text())
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
    flag, on_action = star_choice(form)
    _set(table, "flag", flag, default=False)
    _set(table, "flag_on_action", on_action, default=True)
    _set(table, "track_expiry", track, default=False)
    _set(table, "expired_folder", expired)
    if create:
        cats[key] = table
    _save(box, doc, shared_path)
    return key


@_one_at_a_time
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

@_one_at_a_time
def save_settings(box: Mailbox, shared_path: Path, form: dict, busy: bool = False) -> str:
    """Save the settings page; returns the mailbox's id afterwards. The display name never changes the id:
    only the field box_id (the folder name) does, which moves the folder. `busy`: a job or test is running."""
    doc, auth, oauth = _settings_doc(box, form)
    login = _login_changes(box, form, auth)
    if (login or any(oauth.values())) and not secrets_writable(box.secrets_path):
        raise EditError(_("%(file)s is not writable, so the login cannot be stored.", file=SECRETS_FILE))
    text = _checked_text(box, doc, shared_path)
    new_id = _new_id(box, form)
    if new_id == box.id:
        _write(_config_file(box), text)
    else:
        _move_mailbox(box, new_id, text, busy)
    secrets = box.workspace.with_name(new_id) / SECRETS_FILE  # it moved along with the folder
    if login:
        write_secrets(secrets, "imap", login)
    if oauth:
        write_secrets(secrets, "oauth", oauth)
    return new_id


def connection_from_form(box: Mailbox, shared_path: Path, form: dict) -> tuple[Config, Credentials]:
    """The connection as the settings form has it, for "Check connection" without saving: the settings the
    sorter would load, and the login – what is typed in, the stored password or secret for an empty field."""
    doc, auth, oauth = _settings_doc(box, form)
    try:
        raw = {**tomllib.loads(tomlkit.dumps(doc)), "classifier": _read_toml(shared_path)["classifier"]}
        cfg = config_from_raw(raw, _config_file(box).name)
    except (ConfigError, tomllib.TOMLDecodeError, KeyError) as e:
        raise EditError(str(e)) from None
    found = stored_credentials(box)
    if "imap_user" in form:  # not sent when the login fields are read-only: the stored ones
        found["imap_user"] = _secret_text(form, "imap_user", _("User"), strip=True)
        found["imap_password"] = _secret_text(form, "imap_password", _("Password")) or found["imap_password"]
    found["oauth_client_secret"] = oauth.get("client_secret") or found["oauth_client_secret"]
    if "refresh_token" in oauth:  # another method, app or tenant: the sign-in belongs to the old one
        found["oauth_refresh_token"] = ""
    creds, missing = _imap_login(dataclasses.replace(box, cfg=cfg), found)
    if missing and auth == "password":
        raise EditError(_("Please enter the user and the password."))
    if missing:
        from ..oauth import PROVIDERS
        raise EditError(_("Sign in with %(provider)s first: \"Save & sign in\".", provider=PROVIDERS[auth].label))
    return cfg, creds


def _settings_doc(box: Mailbox, form: dict) -> tuple[tomlkit.TOMLDocument, str, dict]:
    """The mailbox.toml as the settings form changes it, field by field checked; with the sign-in method and
    the changes for the [oauth] secrets (see _sign_in_method)."""
    doc = _doc(box)
    name = _text(form, "name", 60)
    if not name:
        raise EditError(_("The display name must not be empty."), "name")
    doc["name"] = name
    imap = doc.setdefault("imap", tomlkit.table())
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError(_("IMAP server: please enter a host name."), "imap_host")
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
        schedule["interval_minutes"] = _number(form, "schedule_minutes", _("Interval in minutes"), 1, 1440,
                                               integer=True)
        if "reconcile_hours" in form:  # likewise
            schedule["reconcile_enabled"] = _checked(form, "reconcile_enabled")
            schedule["reconcile_hours"] = _number(form, "reconcile_hours", _("Interval in hours"), 1, 720,
                                                  integer=True)
    rules["lookback_days"] = _number(form, "lookback_days", _("Look-back in days"), 1, 365, integer=True)
    rules["max_per_run"] = _number(form, "max_per_run", _("Max. mails per run"), 1, 5000, integer=True)
    _set(rules, "expired_folder", _folder(form, "expired_folder", _("Default folder for expired mail")))
    return doc, auth, oauth


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
        raise EditError(_("Unknown sign-in method."), "imap_auth")
    _set(imap, "auth", auth, default="password")
    if auth == "password":
        return auth, {}
    client_id = _text(form, "oauth_client_id", 200)  # Microsoft: empty = Sortroom's own app
    if (client_id or auth == "google") and not _CLIENT_ID_RE.match(client_id):
        raise EditError(_("Client ID: please copy it from your OAuth app."), "oauth_client_id")
    _set(imap, "oauth_client_id", client_id, default="")
    tenant = _text(form, "oauth_tenant", 100) if auth == "microsoft" else ""
    if tenant and not _TENANT_RE.match(tenant):
        raise EditError(_("Tenant: \"common\", \"consumers\", \"organizations\" or your tenant's ID or domain."),
                        "oauth_tenant")
    _set(imap, "oauth_tenant", tenant, default="")
    oauth: dict[str, str | None] = {}
    secret = _secret_text(form, "oauth_client_secret", _("Client secret"), strip=True) if auth == "google" else ""
    if secret:
        oauth["client_secret"] = secret
    if before is None or (before.imap_auth, before.oauth_client_id, before.oauth_tenant) != (auth, client_id, tenant):
        oauth["refresh_token"] = None
    return auth, oauth


def _new_id(box: Mailbox, form: dict) -> str:
    """The folder name asked for in the form; the current one when the form has none."""
    new_id = _text(form, "box_id", 40).lower() if "box_id" in form else box.id
    if new_id == box.id:
        return new_id
    if not valid_mailbox_id(new_id):
        raise EditError(_("Folder name: lowercase letters, digits, - and _ only, starting with a letter or digit "
                          "(max. 40)."), "box_id")
    if box.workspace.with_name(new_id).exists():
        raise EditError(_("Folder name: mailboxes/%(id)s exists already.", id=new_id), "box_id")
    return new_id


def _move_mailbox(box: Mailbox, new_id: str, text: str, busy: bool) -> None:
    """Rename the mailbox folder to new_id and save its settings there - both or neither."""
    target = box.workspace.with_name(new_id)
    file_name = _config_file(box).name  # before the move: afterwards the old path is gone
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
            _write(target / file_name, text)
        except OSError as e:
            os.rename(target, box.workspace)
            raise EditError(_("Not saved: %(e)s", e=e)) from None
    jobs.rename_mailbox(box.id, new_id)


@_one_at_a_time
def save_rules(box: Mailbox, shared_path: Path, sender_rules: list[tuple[str, str]], delete_rules: list[dict]) -> None:
    """The Rules page: replace the sender rules and the deletion rules, both or neither."""
    doc = _doc(box)
    if _set_sender_rules(doc, sender_rules) | _set_delete_rules(doc, delete_rules):
        _save(box, doc, shared_path)


def config_with_delete_rules(box: Mailbox, shared_path: Path, rules: list[dict]) -> Config:
    """The mailbox's settings with the deletion rules as the Rules page has them, checked like for saving –
    for a dry run of rules that are not saved yet."""
    doc = _doc(box)
    _set_delete_rules(doc, rules)
    try:
        raw = {**tomllib.loads(tomlkit.dumps(doc)), "classifier": _read_toml(shared_path)["classifier"]}
        return config_from_raw(raw, _config_file(box).name)
    except (ConfigError, tomllib.TOMLDecodeError, KeyError) as e:
        raise EditError(str(e)) from None


@_one_at_a_time
def save_sender_rules(box: Mailbox, shared_path: Path, rules: list[tuple[str, str]]) -> None:
    """Replace all sender rules (order kept)."""
    doc = _doc(box)
    if _set_sender_rules(doc, rules):
        _save(box, doc, shared_path)


def _set_sender_rules(doc: tomlkit.TOMLDocument, rules: list[tuple[str, str]]) -> bool:
    """Put the sender rules into the document; False when they were the same already."""
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
    return _replace_tables(doc, "sender_rules", [{"match": m, "action": a} for m, a in clean])


def _replace_tables(doc: tomlkit.TOMLDocument, key: str, rows: list[dict]) -> bool:
    """Make the array of tables `key` hold `rows`, in order; False when it already did (the file then stays
    as it is). Unchanged entries keep their own comments."""
    old = doc.get(key)
    if [dict(t) for t in old or []] == rows:
        return False
    # A comment block before the next section is parsed as the tail of the last entry; take it
    # off and put it back after the new last entry, so it stays in front of that section.
    trailing = _detach_trailing(old[-1]) if old else []
    unused = list(old) if old else []
    aot = AoT([])
    for row in rows:
        same = next((t for t in unused if dict(t) == row), None)
        if same is not None:
            unused.remove(same)
            aot.append(same)
        else:
            t = tomlkit.table()
            for name, value in row.items():
                t[name] = value
            aot.append(t)
    if rows:
        aot[-1].value.body.extend(trailing)
        doc[key] = aot
    elif old is not None:
        idx = next(i for i, (k, _) in enumerate(doc.body) if k and k.key == key)
        before = doc.body[idx - 1][1] if idx else None
        del doc[key]
        if isinstance(before, Table):
            before.value.body.extend(trailing)
    return True


@_one_at_a_time
def save_delete_rules(box: Mailbox, shared_path: Path, rules: list[dict]) -> None:
    """Replace all deletion rules: dicts with folder, days, only_read, starred. Empty folders are skipped."""
    doc = _doc(box)
    if _set_delete_rules(doc, rules):
        _save(box, doc, shared_path)


def _set_delete_rules(doc: tomlkit.TOMLDocument, rules: list[dict]) -> bool:
    """Put the deletion rules into the document; False when they were the same already."""
    source = str((doc.get("imap") or {}).get("source_folder", "INBOX")).strip("/")
    clean: list[dict] = []
    for r in rules:
        folder = str(r.get("folder") or "").strip().strip("/")
        if not folder:
            continue
        if len(folder) > 200 or not _FOLDER_RE.match(folder):
            raise EditError(_("Deletion rule: invalid folder name \"%(folder)s\".", folder=folder[:40]))
        if folder.lower() == source.lower():
            raise EditError(_("The inbox can't have a deletion rule."))
        try:
            days = int(str(r.get("days") or "").strip())
        except ValueError:
            days = 0
        if not 1 <= days <= DELETE_MAX_DAYS:
            raise EditError(_("Deletion rule for %(folder)s: the age is a whole number of days from 1 to %(max)s.",
                              folder=folder, max=DELETE_MAX_DAYS))
        row: dict = {"folder": folder, "days": days}
        if r.get("only_read"):
            row["only_read"] = True
        if r.get("starred"):
            row["starred"] = True
        clean.append(row)
    return _replace_tables(doc, "delete_rules", clean)


def _detach_trailing(table: Table) -> list:
    body = table.value.body
    end = len(body)
    while end and body[end - 1][0] is None:
        end -= 1
    tail = body[end:]
    del body[end:]
    return tail


@_one_at_a_time
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


@_one_at_a_time
def rename_folder_refs(box: Mailbox, shared_path: Path, old: str, new: str) -> int:
    """Point category folders, expired folders and deletion rules below `old` to `new`. Returns how many changed."""
    doc = _doc(box)
    changed = 0
    tables = ([doc.get("rules") or {}] + list((doc.get("categories") or {}).values())
              + list(doc.get("delete_rules") or []))
    for table in tables:
        for key in ("folder", "expired_folder"):
            renamed = _renamed_path(table.get(key), old, new)
            if renamed:
                table[key] = renamed
                changed += 1
    if changed:
        _save(box, doc, shared_path)
    return changed


@_one_at_a_time
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


@_one_at_a_time
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
        raise EditError(_("IMAP server: please enter a host name."), "imap_host")
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


@_one_at_a_time
def save_shared(base_dir: Path, shared_path: Path, form: dict) -> None:
    """Update [classifier] in config.toml; every mailbox must still load with it."""
    from ..config import load_mailboxes

    if not shared_writable(shared_path):
        raise EditError(_("%(file)s is read-only.", file=shared_path.name))
    doc = tomlkit.parse(shared_path.read_text(encoding="utf-8"))
    values, key = classifier_from_form(form)
    for name, value in values.items():
        doc["classifier"][name] = value
    language = _text(form, "language", 10) or DEFAULT_LANGUAGE
    if language not in LANGUAGES:
        raise EditError(_("Unknown language."))
    if "ui" not in doc:
        doc["ui"] = tomlkit.table()
    doc["ui"]["language"] = language
    theme = _text(form, "theme", 10) or "auto"
    if theme not in THEMES:
        raise EditError(_("Unknown appearance."))
    doc["ui"]["theme"] = theme
    secrets = shared_path.with_name(SECRETS_FILE)
    if key and not secrets_writable(secrets):
        raise EditError(_("%(file)s is not writable, so the API key cannot be stored.", file=SECRETS_FILE))
    smtp_password = _mail_settings(doc, form)
    if smtp_password and not secrets_writable(secrets):
        raise EditError(_("%(file)s is not writable, so the SMTP password cannot be stored.", file=SECRETS_FILE))

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
    if smtp_password:
        write_secrets(secrets, "smtp", {"password": smtp_password})


def classifier_from_form(form: dict) -> tuple[dict, str]:
    """[classifier] from the Global settings form, checked, and the API key typed in – write-only: empty
    keeps the stored one. For saving, and for "Check model", which tries them without saving."""
    values: dict = {}
    for field, label in (("endpoint", _("Endpoint")), ("model", _("Model"))):
        value = _text(form, field, 300)
        if not value or " " in value:
            raise EditError(_("%(label)s: please fill in.", label=label), field)
        values[field] = value
    if not values["endpoint"].startswith("https://"):
        raise EditError(_("Endpoint: please enter an https address."), "endpoint")
    values["max_body_chars"] = _number(form, "max_body_chars", _("Mail text length"), 200, 20000, integer=True)
    values["timeout_seconds"] = _number(form, "timeout_seconds", _("Timeout in seconds"), 1, 300)
    values["min_interval_seconds"] = _number(form, "min_interval_seconds", _("Minimum interval in seconds"), 0, 60)
    return values, _secret_text(form, "api_key", _("API key"), strip=True)


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _mail_settings(doc: tomlkit.TOMLDocument, form: dict) -> str:
    """[mail] from the form into the document; returns the new SMTP password, if any."""
    values, password = mail_from_form(form)
    if values["host"] or "mail" in doc:
        mail = doc.setdefault("mail", tomlkit.table())
        for name, item in values.items():
            mail[name] = item
    return password


def mail_from_form(form: dict) -> tuple[dict, str]:
    """[mail] from the Global settings form (notifications and the daily summary), checked, and the SMTP
    password typed in ("" keeps the stored one). For saving, and for "Send test mail"."""
    from email.utils import parseaddr

    from ..mail import SECURITY

    host = _text(form, "smtp_host", 200)
    sender, recipient = _text(form, "mail_sender", 300), _text(form, "mail_recipient", 300)
    notify, digest = _flag(form, "notify_failures"), _flag(form, "digest")
    ui_url = _text(form, "ui_url", 300)
    if (notify or digest) and not (host and sender and recipient):
        missing = next(name for name, value in (("smtp_host", host), ("mail_sender", sender),
                                                ("mail_recipient", recipient)) if not value)
        raise EditError(_("Mail: to send notifications or the summary, please fill in the SMTP server, From and To."),
                        missing)
    for value, label, field in ((sender, _("From"), "mail_sender"), (recipient, _("To"), "mail_recipient")):
        if value and "@" not in parseaddr(value)[1]:
            raise EditError(_("%(label)s: please enter an e-mail address.", label=label), field)
    if ui_url and not ui_url.startswith(("http://", "https://")):
        raise EditError(_("Address of this interface: please start with http:// or https://."), "ui_url")
    security = _text(form, "smtp_security", 10) or "starttls"
    if security not in SECURITY:
        raise EditError(_("Unknown encryption."), "smtp_security")
    digest_time = _text(form, "digest_time", 5) or "07:00"
    if not _TIME_RE.match(digest_time):
        raise EditError(_("Daily summary: please enter a time such as 07:00."), "digest_time")
    port = _number(form, "smtp_port", _("Port"), 1, 65535, integer=True) if _text(form, "smtp_port", 6) else 587
    values = {"host": host, "port": port, "security": security, "user": _text(form, "smtp_user", 200),
              "sender": sender, "recipient": recipient, "ui_url": ui_url, "notify_failures": notify,
              "digest": digest, "digest_time": digest_time}
    return values, _secret_text(form, "smtp_password", _("Password"))


def _flag(form: dict, name: str) -> bool:
    return form.get(name) in ("1", "on", "true")


# ---------------------------------------------------------------- secrets (logins, API key)

_SECRET_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")


def _secret_text(form: dict, name: str, label: str, strip: bool = False) -> str:
    """A login or key field. Its value never goes into an error message."""
    value = str(form.get(name) or "")
    value = value.strip() if strip else value
    if value and not _SECRET_RE.match(value):
        raise EditError(_("%(label)s: at most 500 characters, no line breaks.", label=label), name)
    return value


def _stored(path: Path, section: str) -> dict:
    try:
        return dict(read_secrets(path).get(section, {}))
    except ConfigError as e:
        raise EditError(str(e)) from None
