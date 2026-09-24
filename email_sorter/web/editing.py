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

_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_FOLDER_RE = re.compile(r"^[^\\%*\x00-\x1f]{1,200}$")  # IMAP list wildcards and control chars excluded


class EditError(ValueError):
    """A change the user has to fix; the file was not touched."""


def writable(box: Mailbox) -> bool:
    return bool(box.config_file) and box.config_file.exists() and os.access(box.config_file, os.W_OK)


def is_single_file(box: Mailbox) -> bool:
    return bool(box.config_file) and box.config_file.name != "mailbox.toml"


def _doc(box: Mailbox) -> tomlkit.TOMLDocument:
    if not writable(box):
        raise EditError("Diese Einstellungen sind schreibgeschützt.")
    return tomlkit.parse(box.config_file.read_text(encoding="utf-8"))


def _save(box: Mailbox, doc: tomlkit.TOMLDocument, shared_path: Path) -> None:
    text = tomlkit.dumps(doc)
    try:
        raw = tomllib.loads(text)
        if is_single_file(box):
            config_from_raw(raw, box.config_file.name)
        else:
            config_from_raw({**raw, "jev": _read_toml(shared_path)["jev"]}, box.config_file.name)
    except (ConfigError, tomllib.TOMLDecodeError, KeyError) as e:
        raise EditError(f"Nicht gespeichert: {e}") from None
    path = box.config_file
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- form parsing

def _text(form: dict, name: str, max_len: int = 4000) -> str:
    value = str(form.get(name, "") or "").strip()
    if len(value) > max_len:
        raise EditError(f"„{name}“ ist zu lang (max. {max_len} Zeichen).")
    return value


def _folder(form: dict, name: str, label: str) -> str:
    value = _text(form, name, 200).strip("/")
    if value and not _FOLDER_RE.match(value):
        raise EditError(f"{label}: ungültiger Ordnername.")
    return value


def _number(form: dict, name: str, label: str, lo: float, hi: float, integer: bool = False):
    raw = _text(form, name, 20).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise EditError(f"{label}: bitte eine Zahl angeben.") from None
    if not lo <= value <= hi:
        raise EditError(f"{label}: erlaubt sind Werte von {lo:g} bis {hi:g}.")
    if integer:
        if value != int(value):
            raise EditError(f"{label}: bitte eine ganze Zahl angeben.")
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
        raise EditError("Schlüssel: nur Kleinbuchstaben, Ziffern und _ (max. 40).")
    doc = _doc(box)
    cats = doc.get("categories")
    if cats is None:
        raise EditError("Die Datei hat keine Kategorien.")
    if create and key in cats:
        raise EditError(f"Die Kategorie „{key}“ gibt es schon.")
    if not create and key not in cats:
        raise EditError(f"Die Kategorie „{key}“ gibt es nicht.")

    description = _text(form, "description")
    if not description:
        raise EditError("Die Beschreibung darf nicht leer sein – Jev ordnet nur nach ihr ein.")
    label = _text(form, "label", 60)
    folder = _folder(form, "folder", "Zielordner")
    track = _checked(form, "track_expiry")
    expired = _folder(form, "expired_folder", "Zielordner für Abgelaufenes") if track else ""

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
        raise EditError(f"Die Kategorie „{key}“ gibt es nicht.")
    used = [r.get("match") for r in doc.get("sender_rules", []) if r.get("action") == key]
    if used:
        raise EditError(f"Wird von Absender-Regeln benutzt ({', '.join(used)}) – diese zuerst ändern.")
    del cats[key]
    _save(box, doc, shared_path)


# ---------------------------------------------------------------- mailbox settings

def save_settings(box: Mailbox, shared_path: Path, form: dict) -> None:
    doc = _doc(box)
    if not is_single_file(box):
        name = _text(form, "name", 60)
        if not name:
            raise EditError("Der Anzeigename darf nicht leer sein.")
        doc["name"] = name
    imap = doc.setdefault("imap", tomlkit.table())
    host = _text(form, "imap_host", 200)
    if not host or " " in host:
        raise EditError("IMAP-Server: bitte einen Hostnamen angeben.")
    imap["host"] = host
    imap["port"] = _number(form, "imap_port", "Port", 1, 65535, integer=True)
    source = _folder(form, "source_folder", "Posteingang")
    imap["source_folder"] = source or "INBOX"

    rules = doc.setdefault("rules", tomlkit.table())
    rules["min_confidence"] = _number(form, "min_confidence", "Mindest-Konfidenz", 0, 1)
    rules["action_flag_threshold"] = _number(form, "action_flag_threshold", "Stern bei Handlungsbedarf", 0, 1)
    rules["expiry_threshold"] = _number(form, "expiry_threshold", "Befristetes Angebot", 0, 1)
    rules["min_age_hours"] = _number(form, "min_age_hours", "Wartezeit", 0, 24 * 14)
    rules["lookback_days"] = _number(form, "lookback_days", "Rückblick", 1, 365, integer=True)
    rules["max_per_run"] = _number(form, "max_per_run", "Max. pro Lauf", 1, 5000, integer=True)
    _set(rules, "expired_folder", _folder(form, "expired_folder", "Standard-Ordner für Abgelaufenes"))
    _save(box, doc, shared_path)


def save_sender_rules(box: Mailbox, shared_path: Path, rules: list[tuple[str, str]]) -> None:
    """Replace all sender rules (order kept). The older keep_in_inbox_from list is folded in."""
    doc = _doc(box)
    categories = doc.get("categories") or {}
    clean: list[tuple[str, str]] = []
    for match, action in rules:
        match = match.strip().lower()
        if not match:
            continue
        if len(match) > 200:
            raise EditError(f"Absender „{match[:40]}…“ ist zu lang.")
        if action != INBOX_ACTION and action not in categories:
            raise EditError(f"Unbekanntes Ziel „{action}“ für {match}.")
        clean.append((match, action))
    old = doc.get("sender_rules")
    legacy = "rules" in doc and "keep_in_inbox_from" in doc["rules"]
    if not legacy and [(t.get("match"), t.get("action")) for t in old or []] == clean:
        return  # nothing changed; leave the file as it is
    if legacy:
        del doc["rules"]["keep_in_inbox_from"]
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
