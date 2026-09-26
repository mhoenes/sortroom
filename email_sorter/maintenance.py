"""One-off maintenance after renaming categories or folders in config.toml.

    python -m email_sorter --rename-category newsletter werbung [--live]
    python -m email_sorter --rename-folder INBOX/Newsletter INBOX/Werbung [--live]
    python -m email_sorter --relocate portal [--live]

Without --live both only report what they would change.
"""
from __future__ import annotations

import logging

from imap_tools import MailBox

from .config import Config, Credentials
from .runtime import BASE_DIR
from .sorter import (IMAP_TIMEOUT, RunResult, move_uids, server_folder, _chunks, _delimiter, _ensure_folder,
                     _find_uids, _group_by_folder)
from .store import Store

log = logging.getLogger(__name__)


def rename_category(old: str, new: str, live: bool, base_dir=BASE_DIR) -> RunResult:
    """Rename a category key in the processed-mail log (the key in config.toml is edited by hand)."""
    store = Store(base_dir / "data" / "state.db")
    try:
        count = store.count_category(old)
        if live:
            store.rename_category(old, new)
            log.info("renamed category %r -> %r in %d record(s)", old, new, count)
        else:
            log.info("dry run: would rename category %r -> %r in %d record(s)", old, new, count)
        return RunResult(exit_code=0, live=live, classified=count)
    finally:
        store.close()


def rename_folder(cfg: Config, creds: Credentials, old: str, new: str, live: bool,
                  base_dir=BASE_DIR) -> RunResult:
    """Rename a folder on the IMAP server (with its subfolders and mail) and in the processed-mail log.

    `old` and `new` use config notation ("INBOX/Newsletter"); the server's separator is applied.
    """
    store = Store(base_dir / "data" / "state.db")
    try:
        with MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            delim = _delimiter(mb)
            old_srv, new_srv = server_folder(old, delim), server_folder(new, delim)
            if not mb.folder.exists(old_srv):
                log.error("folder %s does not exist on the server", old_srv)
                return RunResult(exit_code=2, error=f"folder {old_srv} not found")
            if mb.folder.exists(new_srv):
                log.error("folder %s already exists - merge by hand or pick another name", new_srv)
                return RunResult(exit_code=2, error=f"folder {new_srv} already exists")

            tree = [old_srv] + sorted(f.name for f in mb.folder.list(old_srv + delim)
                                      if f.name.startswith(old_srv + delim))
            renamed = [new_srv + name[len(old_srv):] for name in tree]
            mails = sum(mb.folder.status(name, ["MESSAGES"])["MESSAGES"] for name in tree)
            records = store.count_moved_to(old)
            for a, b in zip(tree, renamed):
                log.info("%s %s -> %s", "renaming" if live else "dry run: would rename", a, b)
            log.info("%s %d mail(s) on the server, %d record(s) in the log",
                     "moving" if live else "would move", mails, records)
            if not live:
                return RunResult(exit_code=0, live=False, moved=mails, classified=records)

            mb.folder.rename(old_srv, new_srv)  # IMAP RENAME takes the subfolders along
            for a, b in zip(tree, renamed):
                try:
                    mb.folder.subscribe(a, False)
                except Exception:  # an old name that was never subscribed
                    pass
                mb.folder.subscribe(b, True)
            store.rename_moved_to(old, new)
            log.info("renamed %s -> %s (%d folder(s), %d mail(s), %d record(s))",
                     old_srv, new_srv, len(tree), mails, records)
            return RunResult(exit_code=0, live=True, moved=mails, classified=records)
    finally:
        store.close()


def relocate_category(cfg: Config, creds: Credentials, category: str, live: bool,
                      base_dir=BASE_DIR) -> RunResult:
    """Move already sorted mails of `category` into the folder config.toml now gives it.

    Uses the log, not Jev: only mails classified with at least min_confidence are moved.
    Mails no longer where the log expects them (deleted or moved by hand) are skipped.
    """
    cat = cfg.categories.get(category)
    if not cat or not cat.folder:
        log.error("category %r does not exist or has no folder", category)
        return RunResult(exit_code=2, error=f"category {category!r} unknown or without folder")
    store = Store(base_dir / "data" / "state.db")
    try:
        rows = store.misplaced(category, cat.folder, cfg.min_confidence)
        log.info("%d %s mail(s) in the log are not in %s", len(rows), category, cat.folder)
        if not rows:
            return RunResult(exit_code=0, live=live)
        with MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            delim = _delimiter(mb)
            target = server_folder(cat.folder, delim)
            moved = missing = 0
            try:
                for folder, wanted in _group_by_folder(rows, cfg, delim).items():
                    if not mb.folder.exists(folder):
                        log.warning("%s no longer exists, skipping %d mail(s)", folder, len(wanted))
                        missing += len(wanted)
                        continue
                    uids = _find_uids(mb, folder, wanted, fallback_days=3650)
                    missing += len(wanted) - len(uids)
                    log.info("%s %d mail(s) %s -> %s (%d not found)", "moving" if live else "would move",
                             len(uids), folder, target, len(wanted) - len(uids))
                    if live and uids:
                        _ensure_folder(mb, target)
                        for chunk in _chunks(list(uids.values())):
                            move_uids(mb, chunk, target)
                        store.set_moved_to(uids, cat.folder)
                    moved += len(uids)
            finally:
                mb.folder.set(cfg.source_folder)
            return RunResult(exit_code=0, live=live, moved=moved, failed=missing)
    finally:
        store.close()
