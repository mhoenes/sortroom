"""Actions on mails from the web UI: accept the model's suggestion, put a mail into another category, or
set or remove its star; for one mail or for several at once (the Mails page's selection).

The mail is found by its message key in the folder the log says it is in, changed on the server and
the log is updated (a move: source 'manual', confidence 1.0). Several mails share one IMAP session and are
moved per folder, as the sorter does.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

from imap_tools import MailMessageFlags

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


def set_star(cfg: Config, creds: Credentials, base_dir: Path, key: str, starred: bool) -> None:
    """Set or remove the star (\\Flagged) of one logged mail, on the server and in the log."""
    store = Store(base_dir / "data" / "state.db")
    try:
        row = store.get(key)
        if row is None:
            raise ManualError(_("This mail is not in the log."))
        folder = row["moved_to"] or cfg.source_folder
        with connect(cfg, creds) as mb:
            where = server_folder(folder, delimiter(mb))
            if not mb.folder.exists(where):
                raise ManualError(_("The folder %(folder)s no longer exists.", folder=folder))
            uids = find_uids(mb, where, {key: row["received"]}, fallback_days=MAX_EXPIRY_AGE_DAYS)
            if key not in uids:
                raise ManualError(_("The mail is no longer in %(folder)s – deleted or moved by hand?", folder=folder))
            mb.flag([uids[key]], MailMessageFlags.FLAGGED, starred)
            mb.folder.set(cfg.source_folder)
        store.set_flagged(key, starred)
        log.info("%s %r by hand", "starred" if starred else "unstarred", row["subject"])
    finally:
        store.close()


def _finish_move(cfg: Config, store: Store, row: dict, category: str, target: str | None) -> None:
    """The log after a mail was put into `category`: the correction (if it changes the model's decision) and the
    manual decision."""
    _note_correction(cfg, store, row, category)
    store.set_manual(row["message_key"], row["category"] if category == INBOX_ACTION else category, target)


# what happened to each mail of a batch
MOVED, UNCHANGED, GONE, NO_FOLDER, SORTED, UNKNOWN, FAILED = (
    "moved", "unchanged", "gone", "no-folder", "sorted", "unknown", "failed")


def move_mails(cfg: Config, creds: Credentials, base_dir: Path, wanted: Mapping[str, str]) -> dict[str, str]:
    """Put several logged mails into categories ("inbox" for the inbox): `wanted` maps message key -> category.
    One IMAP session, the mails moved per folder. Returns {key: outcome}: MOVED, UNCHANGED (already there), GONE
    (no longer in its folder, or the folder is), UNKNOWN (not in the log) or FAILED:<reason>. Raises when the
    mailbox can't be reached at all."""
    store = Store(base_dir / "data" / "state.db")
    try:
        outcomes: dict[str, str] = {}
        todo: list[tuple[str, dict, str, str | None]] = []
        for key, category in wanted.items():
            row = store.get(key)
            if row is None:
                outcomes[key] = UNKNOWN
                continue
            target = target_for(cfg, category)
            if (row["moved_to"] or None) == (target or None):  # already there: the log still learns the decision
                _finish_move(cfg, store, row, category, target)
                outcomes[key] = UNCHANGED
            else:
                todo.append((key, row, category, target))
        if todo:
            with connect(cfg, creds) as mb:
                delim = delimiter(mb)
                by_source: dict[str, list] = defaultdict(list)
                for item in todo:
                    by_source[server_folder(item[1]["moved_to"] or cfg.source_folder, delim)].append(item)
                for src, items in by_source.items():
                    if not mb.folder.exists(src):
                        outcomes.update({item[0]: GONE for item in items})
                        continue
                    found = find_uids(mb, src, {item[0]: item[1]["received"] for item in items},
                                      fallback_days=MAX_EXPIRY_AGE_DAYS)
                    by_target: dict[str, list] = defaultdict(list)
                    for key, row, category, target in items:
                        if key in found:
                            by_target[server_folder(target or cfg.source_folder, delim)].append(
                                (key, row, category, target, found[key]))
                        else:
                            outcomes[key] = GONE
                    for dst, moving in by_target.items():  # the mailbox still has `src` selected
                        try:
                            ensure_folder(mb, dst)
                            move_uids(mb, [m[4] for m in moving], dst)
                        except Exception as e:
                            log.warning("moving %d mails from %s to %s failed: %s", len(moving), src, dst, e)
                            outcomes.update({m[0]: f"{FAILED}:{e}" for m in moving})
                            continue
                        log.info("moved %d mails by hand: %s -> %s", len(moving), src, dst)
                        for key, row, category, target, _uid in moving:
                            _finish_move(cfg, store, row, category, target)
                            outcomes[key] = MOVED
                mb.folder.set(cfg.source_folder)
        return outcomes
    finally:
        store.close()


def accept_mails(cfg: Config, creds: Credentials, base_dir: Path, keys: Iterable[str]) -> dict[str, str]:
    """Accept the model's suggestion for several mails: each goes into the folder of the category the log has. A
    mail that is already sorted (SORTED), or whose category has no folder (NO_FOLDER: it would stay where it is),
    is left alone. Otherwise as move_mails."""
    store = Store(base_dir / "data" / "state.db")
    try:
        outcomes: dict[str, str] = {}
        wanted: dict[str, str] = {}
        for key in keys:
            row = store.get(key)
            if row is None:
                outcomes[key] = UNKNOWN
            elif row["moved_to"]:
                outcomes[key] = SORTED
            elif not target_for(cfg, row["category"]):
                outcomes[key] = NO_FOLDER
            else:
                wanted[key] = row["category"]
    finally:
        store.close()
    if wanted:
        outcomes.update(move_mails(cfg, creds, base_dir, wanted))
    return outcomes


def set_stars(cfg: Config, creds: Credentials, base_dir: Path, keys: Iterable[str], starred: bool) -> dict[str, str]:
    """Set or remove the star of several logged mails, per folder in one IMAP session. {key: outcome}: MOVED
    stands for done here, GONE (not found in its folder) or UNKNOWN (not in the log)."""
    store = Store(base_dir / "data" / "state.db")
    try:
        outcomes: dict[str, str] = {}
        by_folder: dict[str, dict[str, str | None]] = defaultdict(dict)
        for key in keys:
            row = store.get(key)
            if row is None:
                outcomes[key] = UNKNOWN
            else:
                by_folder[row["moved_to"] or cfg.source_folder][key] = row["received"]
        if by_folder:
            with connect(cfg, creds) as mb:
                delim = delimiter(mb)
                for folder, wanted in by_folder.items():
                    where = server_folder(folder, delim)
                    if not mb.folder.exists(where):
                        outcomes.update({key: GONE for key in wanted})
                        continue
                    found = find_uids(mb, where, wanted, fallback_days=MAX_EXPIRY_AGE_DAYS)
                    for key in wanted:
                        outcomes[key] = MOVED if key in found else GONE
                    if found:
                        mb.flag(list(found.values()), MailMessageFlags.FLAGGED, starred)
                        for key in found:
                            store.set_flagged(key, starred)
                mb.folder.set(cfg.source_folder)
        return outcomes
    finally:
        store.close()


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
        _finish_move(cfg, store, row, category, target)
        return target
    finally:
        store.close()
