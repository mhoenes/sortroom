"""Compare the processed-mail log with what is actually in the mailbox.

Mails you deleted (or that sit in the trash) are marked `gone`: they stay in the log, so cost and
history are kept and nothing is classified twice, but they no longer show up for review. Mails the
sorter left in the inbox and you filed by hand get the folder they are in now. The unsubscribe links
of logged mails are noted for the Subscriptions page, and their senders' display names. A mail the model
filed that you moved into the folder of another category, or back into the inbox, counts as corrected
for the Categories page.

Reads a few header lines of every mail in every folder; changes nothing on the server.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from imap_tools import MailBox

from .config import Config, Credentials
from .i18n import _
from .mailtext import message_key, received_of, sender_name, unsubscribe_links
from .imap import connect, delimiter, header_fields
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


def _corrections(cfg: Config, store: Store, found: dict[str, str | None]) -> list[tuple[str, str, str | None]]:
    """(key, model category, corrected to or None) for the model's decisions, from where the mails are now.

    Corrected: in the folder of another category, or back in the inbox. Mails in other folders (an
    archive, say) or only in a view count neither way; None means the mail is where the model put it."""
    source = cfg.source_folder.strip("/")
    by_folder: dict[str, list[str]] = {}  # folder -> its categories (several can share one)
    for name, c in cfg.categories.items():
        if c.folder:
            by_folder.setdefault(c.folder.strip("/"), []).append(name)
    out: list[tuple[str, str, str | None]] = []
    for key, category, moved_to in store.model_decisions(cfg.min_confidence):
        where = found.get(key)
        if where is None:
            continue
        # where the model's category puts mail (the log's place is updated when you file by hand)
        cat = cfg.categories.get(category)
        expected = ((cat.folder if cat else moved_to) or source).strip("/")
        others = by_folder.get(where, [])
        if where == expected or category in others:
            out.append((key, category, None))
        elif where == source:
            out.append((key, category, "inbox"))
        elif others:
            out.append((key, category, others[0]))
    return out


def run_reconcile(cfg: Config, creds: Credentials, base_dir: Path, live: bool) -> dict:
    store = Store(base_dir / "data" / "state.db")
    try:
        with connect(cfg, creds) as mb:
            delim = delimiter(mb)
            source = cfg.source_folder.strip("/")
            found: dict[str, str | None] = {}  # message key -> folder (config notation); None: only in a view
            links: dict[str, tuple] = {}  # message key -> (sender, links, one-click?, date), for the senders list
            names: dict[str, str] = {}    # message key -> the sender's display name
            folders, views = mail_folders(mb)
            for name in folders + views:  # real folders first, so they win as a mail's place
                path = name.replace(delim, "/") if delim else name
                mb.folder.set(name)
                n = 0
                for head in header_fields(mb):
                    key = message_key(head)
                    found.setdefault(key, None if name in views else path)
                    names.setdefault(key, sender_name(head))
                    unsubscribe = unsubscribe_links(head)
                    if unsubscribe:
                        links[key] = (head.from_, *unsubscribe, received_of(head))
                    n += 1
                log.info("%s: %d mail(s)%s", path, n, " (view, counts as kept)" if name in views else "")
            mb.folder.set(cfg.source_folder)

        gone, back, filed, logged = [], [], {}, set()
        for key, moved_to, was_gone in store.locations():
            logged.add(key)
            if key not in found:
                if not was_gone:
                    gone.append(key)
                continue
            if was_gone:
                back.append(key)
            where = found[key]
            if moved_to is None and where and where != source:
                filed[key] = where  # left in the inbox by the sorter, filed by hand since
        corrected = _corrections(cfg, store, found)
        log.info("%d mail(s) filed by the model were moved by hand into another category",
                 sum(1 for c in corrected if c[2]))
        log.info("%d folder(s), %d mail(s) on the server; log: %d no longer there, %d back again, "
                 "%d filed by hand", len(folders), len(found), len(gone), len(back), len(filed))
        if live:
            store.set_gone(gone, True)
            store.set_gone(back, False)
            for key, where in filed.items():
                store.set_moved_to([key], where)
            store.note_unsubscribe(v for k, v in links.items() if k in logged)
            store.set_sender_names({k: v for k, v in names.items() if k in logged})
            for key, category, corrected_to in corrected:
                store.set_correction(key, category, corrected_to, "reconcile")
            store.set_meta("last_reconcile", datetime.now().isoformat(timespec="seconds"))
        else:
            log.info("dry run - log not changed")
        summary = _("%(gone)s mail(s) no longer in the mailbox, %(back)s back again, %(filed)s filed by hand",
                    gone=len(gone), back=len(back), filed=len(filed))
        if not live:
            summary += " – " + _("dry run, nothing changed")
        return {"ok": True, "live": live, "gone": len(gone), "back": len(back), "filed": len(filed),
                "summary": summary}
    finally:
        store.close()
