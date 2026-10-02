"""Undo a live run:  python -m email_sorter --undo-run 2026-09-30T10:12:00 [--sort-again] [--live]

The admin UI lists the mails of a run and can undo only some of them (`keys`).

Every live run, backfill and re-sort notes in the log which mails it sorted, the folder they came
from and how their log entry looked before. Undo uses that:
- the mails go back to the folder they came from (the inbox, or the re-sorted folder) and lose the
  star the run gave them;
- mails the log knew before get their old entry back; mails the run saw first stay in the log as
  "in the inbox" and are not sorted again - or, with sort_again, leave the log so the next run
  sorts them anew;
- mails that changed since are left alone: moved or deleted by you, corrected in the UI, sorted
  again by a later run, or moved to the expired folder (expired offers are not brought back).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Mapping
from datetime import datetime
from pathlib import Path

from imap_tools import MailMessageFlags

from .config import Config, Credentials
from .i18n import _
from .imap import chunks, connect, delimiter, ensure_folder, find_uids, group_by_folder, move_uids, server_folder
from .sorter import RunResult, finish
from .store import MOVED, Store

log = logging.getLogger(__name__)


def changed_since(row: Mapping | None, later: bool, moved_to: str | None) -> str | None:
    """Why a mail of a run can't be undone any more, or None while it is still as the run left it
    (`row`: its log entry now, `later`: a later run sorted it again, `moved_to`: where the run put it)."""
    if row is None:
        return "forgotten"   # no longer in the log
    if later:
        return "later"
    if row["gone"]:
        return "gone"        # deleted, or in no folder at the last reconcile
    if row["expired_tagged"] == MOVED:
        return "expired"
    if row["source"] == "manual":
        return "manual"      # corrected by hand
    if row["moved_to"] != moved_to:
        return "moved"       # moved by you, seen by the reconcile
    return None


def _unchanged(entry: dict) -> bool:
    return changed_since(entry["row"], entry["later"], entry["moved_to"]) is None


def run_undo(cfg: Config, creds: Credentials, base_dir: Path, run: str, live: bool,
             sort_again: bool = False, keys: Collection[str] | None = None) -> dict:
    """Undo the live run that started at `run` (as in the run log), or only its mails in `keys`; the others
    can still be undone later. Changes nothing without `live`."""
    store = Store(base_dir / "data" / "state.db")
    started = datetime.now()
    try:
        if run == "last":
            run = next(iter(store.undoable_runs()), "")
        entries = store.undo_entries(run)
        if not entries:
            log.error("no run started at %r that can be undone", run)
            return {"ok": False, "live": live, "exit_code": 2,
                    "summary": _("This run cannot be undone (any more).")}
        left_out: set[str] = set()
        if keys is not None:
            left_out = {e["key"] for e in entries} - set(keys)
            entries = [e for e in entries if e["key"] not in left_out]
            if not entries:
                return {"ok": False, "live": live, "exit_code": 2,
                        "summary": _("None of the chosen mails belongs to this run.")}
            log.info("only %d chosen mail(s) of the run", len(entries))
        todo = [e for e in entries if _unchanged(e)]
        changed = len(entries) - len(todo)
        for e in todo:
            before = e["before"]
            e["move"] = e["moved_to"] != e["origin"]
            e["unflag"] = bool(e["row"]["flagged"]) and not (before and before["flagged"])
        log.info("run of %s sorted %d mail(s); %d changed since and stay as they are", run, len(entries), changed)

        on_server = {e["key"]: e for e in todo if e["move"] or e["unflag"]}
        missing: set[str] = set()   # moved or deleted by you since
        failed: set[str] = set()
        moved = unflagged = 0
        if on_server:
            with connect(cfg, creds) as mb:
                delim = delimiter(mb)
                rows = [(k, e["moved_to"], e["row"]["received"]) for k, e in on_server.items()]
                try:
                    for folder, wanted in group_by_folder(rows, cfg, delim).items():
                        if not mb.folder.exists(folder):
                            log.warning("%s no longer exists, skipping %d mail(s)", folder, len(wanted))
                            missing.update(wanted)
                            continue
                        uids = find_uids(mb, folder, wanted, fallback_days=3650)
                        missing.update(set(wanted) - set(uids))
                        star = [uid for k, uid in uids.items() if on_server[k]["unflag"]]
                        back: dict[str, dict[str, str]] = defaultdict(dict)  # target -> {key: uid}
                        for k, uid in uids.items():
                            if on_server[k]["move"]:
                                back[server_folder(on_server[k]["origin"] or cfg.source_folder, delim)][k] = uid
                        for target, group in back.items():
                            log.info("%s %d mail(s) %s -> %s (%d not found)", "moving" if live else "would move",
                                     len(group), folder, target, len(wanted) - len(uids))
                        if star:
                            log.info("%s the star of %d mail(s) in %s", "removing" if live else "would remove",
                                     len(star), folder)
                        if not live:
                            moved += sum(len(g) for g in back.values())
                            unflagged += len(star)
                            continue
                        try:
                            for chunk in chunks(star):  # before moving: a move gives the mails new UIDs
                                mb.flag(chunk, MailMessageFlags.FLAGGED, False)
                            unflagged += len(star)
                        except Exception as e:
                            log.error("removing stars in %s failed: %s", folder, e)
                            failed.update(k for k, uid in uids.items() if uid in star)
                        for target, group in back.items():
                            try:
                                ensure_folder(mb, target)
                                for chunk in chunks(list(group.values())):
                                    move_uids(mb, chunk, target)
                                moved += len(group)
                            except Exception as e:
                                log.error("moving back to %s failed, undo can be tried again: %s", target, e)
                                failed.update(group)
                finally:
                    mb.folder.set(cfg.source_folder)

        handled = [e for e in todo if e["key"] not in missing and e["key"] not in failed]
        if live:
            restore, forget, back_to = [], [], {}
            for entry in handled:
                if entry["before"]:
                    restore.append({**entry["before"], "moved_to": entry["origin"]})
                elif sort_again:
                    forget.append(entry["key"])
                else:
                    back_to[entry["key"]] = entry["origin"]
            store.undo(run, restore, forget, back_to, keep=failed | left_out)
            finish(store, "undo", run, started, RunResult(
                exit_code=1 if failed else 0, live=True, classified=len(handled), moved=moved, failed=len(failed)))
            if forget:
                log.info("%d mail(s) will be sorted again at the next run", len(forget))
        summary = _("%(moved)s mail(s) moved back, %(unflagged)s star(s) removed, %(skipped)s changed since "
                    "and left alone", moved=moved, unflagged=unflagged, skipped=changed + len(missing))
        if failed:
            summary += " – " + _("%(failed)s could not be changed, see the log", failed=len(failed))
        if not live:
            summary += " – " + _("dry run, nothing changed")
        return {"ok": not failed, "live": live, "exit_code": 1 if failed else 0, "moved": moved,
                "unflagged": unflagged, "skipped": changed + len(missing), "failed": len(failed),
                "summary": summary}
    finally:
        store.close()
