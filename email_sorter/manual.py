"""Single-mail actions from the web UI: accept the model's suggestion or put a mail into another category.

The mail is found by its message key in the folder the log says it is in, moved on the server and
the log is updated (source 'manual', confidence 1.0).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import INBOX_ACTION, Config, Credentials
from .i18n import _
from .imap import connect, delimiter, ensure_folder, find_uids, move_uids, server_folder
from .sorter import MAX_EXPIRY_AGE_DAYS
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
        raise ManualError(_("Unknown category \"%(key)s\".", key=category))
    return cat.folder


def _note_correction(cfg: Config, store: Store, row: dict, category: str) -> None:
    """Count a changed decision of the model for the correction rate (Categories page)."""
    before = store.correction(row["message_key"])
    if before:
        model = before["category"]
    elif row["source"] == "classifier" and row["confidence"] >= cfg.min_confidence:
        model = row["category"]
    else:
        return  # a sender rule, an uncertain suggestion, or a mail you had already set by hand
    stays = category == model or (category == INBOX_ACTION and not target_for(cfg, model))
    store.set_correction(row["message_key"], model, None if stays else category, "ui")


def move_mail(cfg: Config, creds: Credentials, base_dir: Path, key: str, category: str) -> str | None:
    """Put one logged mail into `category` (or back into the inbox with 'inbox'). Returns its folder."""
    store = Store(base_dir / "data" / "state.db")
    try:
        row = store.get(key)
        if row is None:
            raise ManualError(_("This mail is not in the log."))
        target = target_for(cfg, category)
        current = row["moved_to"]
        if (current or None) != (target or None):
            with connect(cfg, creds) as mb:
                delim = delimiter(mb)
                src = server_folder(current or cfg.source_folder, delim)
                dst = server_folder(target or cfg.source_folder, delim)
                if not mb.folder.exists(src):
                    raise ManualError(_("The folder %(folder)s no longer exists.", folder=current or cfg.source_folder))
                uids = find_uids(mb, src, {key: row["received"]}, fallback_days=MAX_EXPIRY_AGE_DAYS)
                if key not in uids:
                    raise ManualError(_("The mail is no longer in %(folder)s – deleted or moved by hand?",
                                        folder=current or cfg.source_folder))
                ensure_folder(mb, dst)
                move_uids(mb, [uids[key]], dst)
                mb.folder.set(cfg.source_folder)
            log.info("moved %r by hand: %s -> %s", row["subject"], src, dst)
        _note_correction(cfg, store, row, category)
        store.set_manual(key, row["category"] if category == INBOX_ACTION else category, target)
        return target
    finally:
        store.close()
