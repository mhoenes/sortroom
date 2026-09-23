"""One sorting run: find new mails, ask Jev, then move/flag (live) or report (dry run)."""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from imap_tools import AND, MailBox, MailMessageFlags

from .config import Config, Credentials
from .jev import Decision, JevAuthError, JevClient, JevError
from .mailtext import build_state, message_key
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    key: str
    uid: str
    received: str
    sender: str
    subject: str
    decision: Decision
    folder: str | None  # config-style path ("INBOX/Finanzen"), None = stays
    flag: bool
    note: str = ""


def plan(decision: Decision, cfg: Config) -> tuple[str | None, bool, str]:
    """Decide what to do with a mail: (target folder, flag?, note)."""
    category = cfg.categories[decision.category]
    confident = decision.confidence >= cfg.min_confidence
    folder = category.folder if confident else None
    flag = (category.flag and confident) or decision.needs_action >= cfg.action_flag_threshold
    note = "" if confident else f"low confidence (< {cfg.min_confidence:.2f}), stays in inbox"
    return folder, flag, note


def server_folder(path: str, delim: str) -> str:
    return delim.join(part for part in path.split("/") if part)


def _delimiter(mb: MailBox) -> str:
    folders = mb.folder.list()
    for f in folders:
        if f.name.upper() == "INBOX" and f.delim:
            return f.delim
    return next((f.delim for f in folders if f.delim), "/")


def _ensure_folder(mb: MailBox, name: str) -> None:
    if not mb.folder.exists(name):
        log.info("creating folder %s", name)
        mb.folder.create(name)
        mb.folder.subscribe(name, True)


def classify_new(
    mb: MailBox,
    cfg: Config,
    jev: JevClient,
    store: Store,
    limit: int,
    on_outcome: Callable[[Outcome], None] | None = None,
) -> tuple[list[Outcome], int]:
    since = date.today() - timedelta(days=cfg.lookback_days)
    pending: dict[str, str] = {}  # uid -> message key
    for head in mb.fetch(AND(date_gte=since), mark_seen=False, headers_only=True, bulk=True):
        key = message_key(head)
        if not store.is_processed(key):
            pending[head.uid] = key

    uids = sorted(pending, key=int, reverse=True)[:limit]  # newest first
    log.info("%d new mail(s) in %s since %s, classifying %d", len(pending), cfg.source_folder, since, len(uids))
    if not uids:
        return [], 0

    outcomes, errors = [], 0
    descriptions = cfg.descriptions
    for msg in mb.fetch(AND(uid=uids), mark_seen=False, bulk=True):
        try:
            decision = jev.decide(build_state(msg, cfg.max_body_chars), descriptions)
        except JevAuthError:
            raise
        except JevError as e:
            errors += 1
            log.warning("could not classify %r: %s", msg.subject, e)
            continue
        folder, flag, note = plan(decision, cfg)
        outcome = Outcome(
            key=pending[msg.uid],
            uid=msg.uid,
            received=msg.date.isoformat(timespec="minutes") if msg.date else msg.date_str,
            sender=msg.from_,
            subject=msg.subject,
            decision=decision,
            folder=folder,
            flag=flag,
            note=note,
        )
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        log.debug("%.2f %-18s %s", decision.confidence, decision.category, msg.subject)
    return outcomes, errors


def apply(mb: MailBox, outcomes: list[Outcome], store: Store) -> int:
    """Flag and move mails on the server; record what succeeded. Returns failure count."""
    delim = _delimiter(mb)
    failures = 0

    to_flag = [o.uid for o in outcomes if o.flag]
    if to_flag:
        try:
            mb.flag(to_flag, MailMessageFlags.FLAGGED, True)
        except Exception as e:  # imap_tools raises various MailboxError subclasses
            log.error("flagging failed: %s", e)
            for o in outcomes:
                o.flag = False

    by_folder: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        if o.folder:
            by_folder[server_folder(o.folder, delim)].append(o)

    failed: set[str] = set()
    for folder, group in by_folder.items():
        try:
            _ensure_folder(mb, folder)
            mb.move([o.uid for o in group], folder)
            log.info("moved %d mail(s) to %s", len(group), folder)
        except Exception as e:
            log.error("moving to %s failed, will retry next run: %s", folder, e)
            failures += len(group)
            failed.update(o.key for o in group)

    for o in outcomes:
        if o.key not in failed:
            store.record(o)
    return failures


class ReportWriter:
    """Dry-run CSV, written row by row so an interrupted run still leaves a report."""

    HEADER = ["received", "from", "subject", "category", "confidence", "runner_up",
              "needs_action", "would_move_to", "would_flag", "note"]

    def __init__(self, reports_dir: Path):
        reports_dir.mkdir(parents=True, exist_ok=True)
        self.path = reports_dir / f"dry-run-{datetime.now():%Y%m%d-%H%M%S}.csv"
        self.rows = 0
        # utf-8-sig + ";" so German Excel opens it correctly with a double click
        self._file = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._csv = csv.writer(self._file, delimiter=";")
        self._csv.writerow(self.HEADER)

    def write(self, o: Outcome) -> None:
        d = o.decision
        ru = d.runner_up
        self._csv.writerow([
            o.received, o.sender, o.subject, d.category, f"{d.confidence:.2f}",
            f"{ru[0]} ({ru[1]:.2f})" if ru else "", f"{d.needs_action:.2f}",
            o.folder or "(inbox)", "yes" if o.flag else "", o.note,
        ])
        self._file.flush()
        self.rows += 1

    def close(self) -> None:
        self._file.close()
        if self.rows == 0:
            self.path.unlink(missing_ok=True)


def summarize(outcomes: list[Outcome], live: bool) -> None:
    if not outcomes:
        return
    verb = "moved" if live else "would move"
    counts = Counter(o.decision.category for o in outcomes)
    moved = sum(1 for o in outcomes if o.folder)
    flagged = sum(1 for o in outcomes if o.flag)
    cost = sum(o.decision.cost for o in outcomes)
    log.info("categories: %s", ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    log.info("%s %d, flagged %d, kept %d in inbox, cost $%.6f",
             verb, moved, flagged, len(outcomes) - moved, cost)


def run(cfg: Config, creds: Credentials, base_dir: Path, live: bool, limit: int | None) -> int:
    """Returns a process exit code."""
    store = Store(base_dir / "data" / "state.db")
    jev = cfg.jev_client(creds.jev_api_key)
    limit = min(limit or cfg.max_per_run, cfg.max_per_run)
    report = None if live else ReportWriter(base_dir / "reports")
    try:
        with MailBox(cfg.imap_host, cfg.imap_port).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            try:
                outcomes, errors = classify_new(mb, cfg, jev, store, limit,
                                                on_outcome=report.write if report else None)
            except JevAuthError as e:
                log.error("%s - check %s in .env and your gateway credits", e, cfg.jev_api_key_env)
                return 2
            failures = 0
            if live:
                failures = apply(mb, outcomes, store)
            elif outcomes:
                log.info("dry run - nothing changed. Report: %s", report.path)
            summarize(outcomes, live)
    finally:
        store.close()
        if report:
            report.close()
    return 1 if (errors or failures) else 0
