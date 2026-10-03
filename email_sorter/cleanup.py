"""Deletion rules: move old mail out of a folder into the trash.  python -m email_sorter --delete-old [--live]

A rule names a folder and an age in days. Mail that arrived on the server (INTERNALDATE) more than that
many days ago goes to the mailbox's trash folder - not deleted for real, so it can be taken back from
there until the trash is emptied. Starred mail stays unless the rule says otherwise; a rule can also
leave unread mail alone. The inbox, the trash, spam, sent, drafts and views like Gmail's "All Mail"
can't be emptied by a rule. Mails a rule moved are marked "no longer in the mailbox" in the log.

The schedule runs the rules once a day; Maintenance runs them by hand, with a dry run first that lists
what would go.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from imap_tools import AND, MailBox

from .config import Config, Credentials, DeleteRule
from .i18n import _
from .imap import UID_CHUNK, chunks, connect, delimiter, server_folder
from .mailtext import message_key
from .reconcile import SKIP_FLAGS, SKIP_NAMES, VIEW_FLAGS
from .sorter import RunResult, finish
from .store import Store

log = logging.getLogger(__name__)

LIMIT = 1000   # mails per run, over all rules; the rest follows next time
SAMPLE = 5     # mails per rule named in the log
TRASH_NAMES = ("trash", "papierkorb", "gelöscht", "gelöschte elemente", "deleted items", "deleted messages")


def _leaf(name: str, delim: str | None) -> str:
    return (name.split(delim)[-1] if delim else name).lower()


def trash_and_protected(mb: MailBox, source_folder: str) -> tuple[str | None, set[str]]:
    """The server's trash folder (special-use \\Trash, else found by name), and the folders a rule
    must not empty: the inbox, trash, spam, sent, drafts and views like Gmail's "All Mail"."""
    folders = mb.folder.list()
    trash = next((f.name for f in folders if "\\trash" in {x.lower() for x in f.flags}), None)
    if trash is None:
        trash = next((f.name for f in folders if _leaf(f.name, f.delim) in TRASH_NAMES), None)
    protected = {source_folder}
    for f in folders:
        flags = {x.lower() for x in f.flags}
        if flags & (SKIP_FLAGS | VIEW_FLAGS) or _leaf(f.name, f.delim) in SKIP_NAMES:
            protected.add(f.name)
    return trash, protected


def criteria(rule: DeleteRule, today: date):
    """IMAP search for the rule: arrived before the cut-off day (BEFORE compares INTERNALDATE)."""
    return AND(date_lt=today - timedelta(days=rule.days), seen=True if rule.only_read else None,
               flagged=None if rule.starred else False)


def run_cleanup(cfg: Config, creds: Credentials, base_dir: Path, live: bool, today: date | None = None) -> dict:
    """Apply the mailbox's deletion rules. Changes nothing without `live`."""
    if not cfg.delete_rules:
        return {"ok": True, "live": live, "exit_code": 0, "moved": 0,
                "summary": _("This mailbox has no deletion rules (Settings).")}
    today = today or date.today()
    store = Store(base_dir / "data" / "state.db")
    started = datetime.now()
    moved, failed, skipped = 0, 0, 0
    try:
        with connect(cfg, creds) as mb:
            delim = delimiter(mb)
            trash, protected = trash_and_protected(mb, server_folder(cfg.source_folder, delim))
            if trash is None:
                log.error("no trash folder found in this mailbox, so the deletion rules did not run")
                return {"ok": False, "live": live, "exit_code": 2, "moved": 0,
                        "summary": _("No trash folder found in this mailbox, so the deletion rules did not run.")}
            try:
                for rule in cfg.delete_rules:
                    folder = server_folder(rule.folder, delim)
                    if folder in protected or folder.upper() == "INBOX" or folder == trash:
                        log.warning("%s: a deletion rule can't empty the inbox, trash, spam, sent or drafts, "
                                    "skipped", folder)
                        skipped += 1
                        continue
                    if not mb.folder.exists(folder):
                        log.warning("%s does not exist, rule skipped", folder)
                        skipped += 1
                        continue
                    if moved >= LIMIT:
                        log.info("limit of %d mails reached, %s follows next time", LIMIT, folder)
                        break
                    mb.folder.set(folder)
                    heads = [h for h in mb.fetch(criteria(rule, today), mark_seen=False, headers_only=True,
                                                 bulk=UID_CHUNK, limit=LIMIT - moved) if h.uid]
                    log.info("%s: %d mail(s) older than %d day(s)%s%s", folder, len(heads), rule.days,
                             ", read only" if rule.only_read else "", ", starred too" if rule.starred else "")
                    for h in heads[:SAMPLE]:
                        log.info("  %s · %s · %s", h.date.date() if h.date else "?", h.from_, h.subject)
                    if len(heads) > SAMPLE:
                        log.info("  … and %d more", len(heads) - SAMPLE)
                    if not live or not heads:
                        moved += len(heads)
                        continue
                    try:
                        for chunk in chunks([h.uid for h in heads if h.uid]):
                            mb.move(chunk, trash)
                        moved += len(heads)
                        store.set_gone([message_key(h) for h in heads], True)
                    except Exception as e:
                        log.error("moving from %s to %s failed: %s", folder, trash, e)
                        failed += len(heads)
            finally:
                mb.folder.set(cfg.source_folder)
        if live:
            store.set_meta("last_cleanup", datetime.now().isoformat(timespec="seconds"))
            finish(store, "cleanup", None, started, RunResult(
                exit_code=1 if failed else 0, live=True, classified=moved + failed, moved=moved, failed=failed))
    finally:
        store.close()
    summary = (_("%(moved)s mail(s) moved to the trash", moved=moved) if live
               else _("%(moved)s mail(s) would go to the trash", moved=moved))
    if skipped:
        summary += " – " + _("%(skipped)s rule(s) skipped, see the log", skipped=skipped)
    if failed:
        summary += " – " + _("%(failed)s could not be moved, see the log", failed=failed)
    if not live:
        summary += " – " + _("dry run, nothing changed")
    return {"ok": not failed, "live": live, "exit_code": 1 if failed else 0, "moved": moved, "failed": failed,
            "skipped": skipped, "summary": summary}
