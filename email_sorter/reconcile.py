"""Compare the processed-mail log with what is actually in the mailbox.

Mails you deleted (or that sit in the trash) are marked `gone`: they stay in the log, so cost and
history are kept and nothing is classified twice, but they no longer show up for review. Mails the
sorter left in the inbox and you filed by hand get the folder they are in now.

Reads every folder's headers; changes nothing on the server.
"""
from __future__ import annotations

import logging
from pathlib import Path

from imap_tools import MailBox

from .config import Config, Credentials
from .mailtext import message_key
from .sorter import IMAP_TIMEOUT, UID_CHUNK, _delimiter
from .store import Store

log = logging.getLogger(__name__)

SKIP_FLAGS = {"\\trash", "\\junk", "\\sent", "\\drafts", "\\noselect", "\\nonexistent"}
# Gmail views that show mail kept elsewhere: they prove a mail still exists, but are not its place
VIEW_FLAGS = {"\\all", "\\important", "\\flagged"}
SKIP_NAMES = {"trash", "papierkorb", "gelöscht", "gelöschte elemente", "deleted items", "deleted messages",
              "spam", "junk", "junk-e-mail", "sent", "gesendet", "gesendete objekte", "gesendete elemente",
              "sent items", "sent messages", "drafts", "entwürfe"}


def mail_folders(mb: MailBox) -> tuple[list[str], list[str]]:
    """Server names of the folders that hold kept mail (no trash, spam, sent or drafts), and of
    views like Gmail's "All Mail" that only show mail (archived Gmail mail lives only there)."""
    out, views, skipped = [], [], []
    for f in mb.folder.list():
        flags = {x.lower() for x in f.flags}
        leaf = f.name.split(f.delim)[-1].lower() if f.delim else f.name.lower()
        if flags & SKIP_FLAGS or leaf in SKIP_NAMES:
            skipped.append(f.name)
        elif flags & VIEW_FLAGS:
            views.append(f.name)
        else:
            out.append(f.name)
    if skipped:
        log.info("not counted as kept mail: %s", ", ".join(skipped))
    return out, views


def run_reconcile(cfg: Config, creds: Credentials, base_dir: Path, live: bool) -> dict:
    store = Store(base_dir / "data" / "state.db")
    try:
        with MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            delim = _delimiter(mb)
            source = cfg.source_folder.strip("/")
            found: dict[str, str | None] = {}  # message key -> folder (config notation); None: only in a view
            folders, views = mail_folders(mb)
            for name in folders + views:  # real folders first, so they win as a mail's place
                path = name.replace(delim, "/") if delim else name
                mb.folder.set(name)
                n = 0
                for head in mb.fetch(mark_seen=False, headers_only=True, bulk=UID_CHUNK):
                    found.setdefault(message_key(head), None if name in views else path)
                    n += 1
                log.info("%s: %d mail(s)%s", path, n, " (view, counts as kept)" if name in views else "")
            mb.folder.set(cfg.source_folder)

        gone, back, filed = [], [], {}
        for key, moved_to, was_gone in store.locations():
            if key not in found:
                if not was_gone:
                    gone.append(key)
                continue
            if was_gone:
                back.append(key)
            where = found[key]
            if moved_to is None and where and where != source:
                filed[key] = where  # left in the inbox by the sorter, filed by hand since
        log.info("%d folder(s), %d mail(s) on the server; log: %d no longer there, %d back again, "
                 "%d filed by hand", len(folders), len(found), len(gone), len(back), len(filed))
        if live:
            store.set_gone(gone, True)
            store.set_gone(back, False)
            for key, where in filed.items():
                store.set_moved_to([key], where)
        else:
            log.info("dry run - log not changed")
        summary = (f"{len(gone)} Mail(s) nicht mehr im Postfach, {len(back)} wieder aufgetaucht, "
                   f"{len(filed)} von Hand einsortiert" + ("" if live else " – Probelauf, nichts geändert"))
        return {"ok": True, "live": live, "gone": len(gone), "back": len(back), "filed": len(filed),
                "summary": summary}
    finally:
        store.close()
