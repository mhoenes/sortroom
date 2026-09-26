"""Single-mail actions from the web UI: accept Jev's suggestion or put a mail into another category.

The mail is found by its message key in the folder the log says it is in, moved on the server and
the log is updated (source 'manual', confidence 1.0).
"""
from __future__ import annotations

import logging
from pathlib import Path

from imap_tools import MailBox

from .config import INBOX_ACTION, Config, Credentials
from .sorter import (IMAP_TIMEOUT, MAX_EXPIRY_AGE_DAYS, move_uids, server_folder, _delimiter, _ensure_folder,
                     _find_uids)
from .store import Store

log = logging.getLogger(__name__)


class ManualError(RuntimeError):
    pass


def target_for(cfg: Config, category: str) -> str | None:
    """Folder a mail of this category belongs in (None: the inbox)."""
    if category == INBOX_ACTION:
        return None
    cat = cfg.categories.get(category)
    if cat is None:
        raise ManualError(f"Unbekannte Kategorie „{category}“.")
    return cat.folder


def move_mail(cfg: Config, creds: Credentials, base_dir: Path, key: str, category: str) -> str | None:
    """Put one logged mail into `category` (or back into the inbox with 'inbox'). Returns its folder."""
    store = Store(base_dir / "data" / "state.db")
    try:
        row = store.get(key)
        if row is None:
            raise ManualError("Diese Mail steht nicht im Protokoll.")
        target = target_for(cfg, category)
        current = row["moved_to"]
        if (current or None) != (target or None):
            with MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT).login(
                creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
            ) as mb:
                delim = _delimiter(mb)
                src = server_folder(current or cfg.source_folder, delim)
                dst = server_folder(target or cfg.source_folder, delim)
                if not mb.folder.exists(src):
                    raise ManualError(f"Der Ordner {current or cfg.source_folder} existiert nicht mehr.")
                uids = _find_uids(mb, src, {key: row["received"]}, fallback_days=MAX_EXPIRY_AGE_DAYS)
                if key not in uids:
                    raise ManualError(f"Die Mail liegt nicht mehr in {current or cfg.source_folder} "
                                      "– gelöscht oder von Hand verschoben?")
                _ensure_folder(mb, dst)
                move_uids(mb, [uids[key]], dst)
                mb.folder.set(cfg.source_folder)
            log.info("moved %r by hand: %s -> %s", row["subject"], src, dst)
        store.set_manual(key, row["category"] if category == INBOX_ACTION else category, target)
        return target
    finally:
        store.close()
