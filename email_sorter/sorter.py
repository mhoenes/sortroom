"""One sorting run: find new mails, ask Jev, then move/flag (live) or report (dry run)."""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from imap_tools import AND, MailBox, MailMessage, MailMessageFlags

from .config import Config, Credentials
from .expiry import resolve_expiry
from .jev import Decision, JevAuthError, JevClient, JevError
from .mailtext import build_state, full_text, message_key, sent_date
from .store import GONE, TAGGED, Store

log = logging.getLogger(__name__)

MAX_EXPIRY_AGE_DAYS = 200  # tracked offers expire within ~180 days of arrival


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
    expires: date | None = None  # last valid day of a time-limited offer


def plan(decision: Decision, cfg: Config) -> tuple[str | None, bool, str]:
    """Decide what to do with a mail: (target folder, flag?, note)."""
    category = cfg.categories[decision.category]
    confident = decision.confidence >= cfg.min_confidence
    folder = category.folder if confident else None
    needs_action = category.flag_on_action and decision.needs_action >= cfg.action_flag_threshold
    flag = (category.flag and confident) or needs_action
    note = "" if confident else f"low confidence (< {cfg.min_confidence:.2f}), stays in inbox"
    return folder, flag, note


def expiry_for(decision: Decision, cfg: Config, msg: MailMessage) -> date | None:
    """End date of a time-limited offer, for categories that track it."""
    if not cfg.categories[decision.category].track_expiry or decision.has_expiry < cfg.expiry_threshold:
        return None
    return resolve_expiry(full_text(msg), sent_date(msg), decision.expiry_window)


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
    since: date,
    before: date | None = None,
    exclude: set[str] | None = None,
    on_outcome: Callable[[Outcome], None] | None = None,
) -> tuple[list[Outcome], list[str]]:
    """Classify unprocessed mail received in [since, before). Returns (outcomes, failed keys)."""
    criteria = AND(date_gte=since, date_lt=before) if before else AND(date_gte=since)
    pending: dict[str, str] = {}  # uid -> message key
    for head in mb.fetch(criteria, mark_seen=False, headers_only=True, bulk=True):
        key = message_key(head)
        if not store.is_processed(key) and not (exclude and key in exclude):
            pending[head.uid] = key

    uids = sorted(pending, key=int, reverse=True)[:limit]  # newest first
    span = f"{since} to {before - timedelta(days=1)}" if before else f"since {since}"
    log.info("%d new mail(s) in %s %s, classifying %d", len(pending), cfg.source_folder, span, len(uids))
    if not uids:
        return [], []

    outcomes: list[Outcome] = []
    failed: list[str] = []
    descriptions = cfg.descriptions
    for msg in mb.fetch(AND(uid=uids), mark_seen=False, bulk=True):
        try:
            decision = jev.decide(build_state(msg, cfg.max_body_chars), descriptions)
        except JevAuthError:
            raise
        except JevError as e:
            failed.append(pending[msg.uid])
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
            expires=expiry_for(decision, cfg, msg),
        )
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        log.debug("%.2f %-18s %s", decision.confidence, decision.category, msg.subject)
    return outcomes, failed


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
              "needs_action", "would_move_to", "would_flag", "expires", "note"]

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
            o.folder or "(inbox)", "yes" if o.flag else "",
            o.expires.strftime("%d.%m.%Y") if o.expires else "", o.note,
        ])
        self._file.flush()
        self.rows += 1

    def close(self) -> None:
        self._file.close()
        if self.rows == 0:
            self.path.unlink(missing_ok=True)


def _since(received_values: Iterable[str | None], fallback_days: int) -> date:
    """Earliest received date of a group of mails, for a narrow IMAP SINCE search."""
    dates = []
    for r in received_values:
        try:
            dates.append(date.fromisoformat((r or "")[:10]))
        except ValueError:
            return date.today() - timedelta(days=fallback_days)
    return min(dates) - timedelta(days=1) if dates else date.today()


def _find_uids(mb: MailBox, folder: str, wanted: dict[str, str | None], fallback_days: int) -> dict[str, str]:
    """Locate mails by message key in a folder: {key: uid}. `wanted` maps key -> received."""
    mb.folder.set(folder)
    found = {}
    since = _since(wanted.values(), fallback_days)
    for head in mb.fetch(AND(date_gte=since), mark_seen=False, headers_only=True, bulk=True):
        key = message_key(head)
        if key in wanted:
            found[key] = head.uid
    return found


def _group_by_folder(rows, cfg: Config, delim: str) -> dict[str, dict[str, str | None]]:
    """rows of (key, moved_to, received) -> {server folder: {key: received}}"""
    groups: dict[str, dict[str, str | None]] = defaultdict(dict)
    for key, moved_to, received in rows:
        groups[server_folder(moved_to or cfg.source_folder, delim)][key] = received
    return groups


def tag_expired(mb: MailBox, cfg: Config, store: Store, today: date | None = None) -> int:
    """Tag offers whose last valid day has passed and move them to expired_folder.

    Returns the number of mails handled. The keyword is set before the move, so it travels along.
    """
    due = store.due_expired(today or date.today())
    if not due:
        return 0
    delim = _delimiter(mb)
    target = server_folder(cfg.expired_folder, delim) if cfg.expired_folder else None
    tagged = 0
    try:
        for folder, wanted in _group_by_folder(due, cfg, delim).items():
            try:
                uids = _find_uids(mb, folder, wanted, fallback_days=MAX_EXPIRY_AGE_DAYS)
                if uids:
                    mb.flag(list(uids.values()), cfg.expired_keyword, True)
                    if target and target != folder:
                        _ensure_folder(mb, target)
                        mb.move(list(uids.values()), target)
                store.mark_tagged(uids, TAGGED)
                store.mark_tagged(set(wanted) - set(uids), GONE)  # deleted or moved away by the user
                tagged += len(uids)
                if uids:
                    log.info("tagged %d expired offer(s) in %s as '%s'%s", len(uids), folder,
                             cfg.expired_keyword, f" and moved them to {target}" if target else "")
            except Exception as e:
                log.error("tagging expired offers in %s failed, will retry next run: %s", folder, e)
    finally:
        mb.folder.set(cfg.source_folder)
    return tagged


def recheck_expiry(mb: MailBox, cfg: Config, jev: JevClient, store: Store) -> int:
    """One-off: work out expiry dates for mails sorted before expiry tracking existed."""
    tracked = [k for k, c in cfg.categories.items() if c.track_expiry]
    todo = store.unchecked_expiry(tracked)
    log.info("%d already sorted mail(s) to check for an expiry date", len(todo))
    delim = _delimiter(mb)
    found = 0
    try:
        for folder, wanted in _group_by_folder(todo, cfg, delim).items():
            uids = _find_uids(mb, folder, wanted, fallback_days=cfg.lookback_days + 1)
            for key in set(wanted) - set(uids):
                store.set_expiry(key, None)  # no longer there
            key_by_uid = {uid: key for key, uid in uids.items()}
            if not key_by_uid:
                continue
            for msg in mb.fetch(AND(uid=list(key_by_uid)), mark_seen=False, bulk=True):
                try:
                    decision = jev.decide(build_state(msg, cfg.max_body_chars), cfg.descriptions)
                except JevAuthError:
                    raise
                except JevError as e:
                    log.warning("could not check %r: %s", msg.subject, e)
                    continue  # stays unchecked, retried next time
                # the category was decided earlier; only the expiry answers matter here
                expires = None
                if decision.has_expiry >= cfg.expiry_threshold:
                    expires = resolve_expiry(full_text(msg), sent_date(msg), decision.expiry_window)
                store.set_expiry(key_by_uid[msg.uid], expires)
                if expires:
                    found += 1
                    log.debug("expires %s: %s", expires, msg.subject)
    finally:
        mb.folder.set(cfg.source_folder)
    log.info("found %d time-limited offer(s) among already sorted mail", found)
    return found


def summarize(outcomes: list[Outcome], live: bool) -> None:
    if not outcomes:
        return
    verb = "moved" if live else "would move"
    counts = Counter(o.decision.category for o in outcomes)
    moved = sum(1 for o in outcomes if o.folder)
    flagged = sum(1 for o in outcomes if o.flag)
    cost = sum(o.decision.cost for o in outcomes)
    log.info("categories: %s", ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    expiring = sum(1 for o in outcomes if o.expires)
    log.info("%s %d, flagged %d, kept %d in inbox, %d time-limited offer(s), cost $%.6f",
             verb, moved, flagged, len(outcomes) - moved, expiring, cost)


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
            since = date.today() - timedelta(days=cfg.lookback_days)
            try:
                outcomes, failed = classify_new(mb, cfg, jev, store, limit, since,
                                                on_outcome=report.write if report else None)
            except JevAuthError as e:
                log.error("%s - check %s in .env and your gateway credits", e, cfg.jev_api_key_env)
                return 2
            failures = 0
            if live:
                failures = apply(mb, outcomes, store)
                tag_expired(mb, cfg, store)
            elif outcomes:
                log.info("dry run - nothing changed. Report: %s", report.path)
            summarize(outcomes, live)
    finally:
        store.close()
        if report:
            report.close()
    return 1 if (failed or failures) else 0


def month_windows(since: date, until: date) -> list[tuple[date, date]]:
    """[since, until) split into calendar months, newest first: [(start, end_exclusive), ...]"""
    windows = []
    end = until
    while end > since:
        start = max(since, (end - timedelta(days=1)).replace(day=1))
        windows.append((start, end))
        end = start
    return windows


def run_backfill(cfg: Config, creds: Credentials, base_dir: Path, live: bool,
                 since: date, limit: int | None) -> int:
    """--since: manually sort older mail, month by month (newest first) in batches.

    Only reachable from the command line; the scheduled task never passes --since.
    Live batches are applied and recorded right away, so an interrupted backfill
    resumes where it stopped.
    """
    store = Store(base_dir / "data" / "state.db")
    jev = cfg.jev_client(creds.jev_api_key)
    report = None if live else ReportWriter(base_dir / "reports")
    remaining = limit
    all_outcomes: list[Outcome] = []
    all_failed: list[str] = []
    failures = 0
    seen: set[str] = set()  # dry run records nothing; don't classify the same mail twice
    try:
        with MailBox(cfg.imap_host, cfg.imap_port).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            for start, end in month_windows(since, date.today() + timedelta(days=1)):
                while remaining is None or remaining > 0:
                    batch = min(cfg.max_per_run, remaining) if remaining is not None else cfg.max_per_run
                    try:
                        outcomes, failed = classify_new(
                            mb, cfg, jev, store, batch, start, before=end, exclude=seen,
                            on_outcome=report.write if report else None,
                        )
                    except JevAuthError as e:
                        log.error("%s - check %s in .env and your gateway credits", e, cfg.jev_api_key_env)
                        return 2
                    seen.update(o.key for o in outcomes)
                    seen.update(failed)  # retried on the next backfill, not in this one
                    if live and outcomes:
                        failures += apply(mb, outcomes, store)
                    all_outcomes.extend(outcomes)
                    all_failed.extend(failed)
                    handled = len(outcomes) + len(failed)
                    if remaining is not None:
                        remaining -= handled
                    if handled < batch:
                        break  # month done
                if remaining is not None and remaining <= 0:
                    log.info("limit reached, stopping")
                    break
            if live:
                tag_expired(mb, cfg, store)
            elif all_outcomes:
                log.info("dry run - nothing changed. Report: %s", report.path)
            summarize(all_outcomes, live)
            if all_failed:
                log.warning("%d mail(s) could not be classified; run the backfill again to retry", len(all_failed))
    finally:
        store.close()
        if report:
            report.close()
    return 1 if (all_failed or failures) else 0


def run_recheck_expiry(cfg: Config, creds: Credentials, base_dir: Path, live: bool) -> int:
    """--recheck-expiry: find expiry dates for already sorted mail, then tag (live only)."""
    store = Store(base_dir / "data" / "state.db")
    jev = cfg.jev_client(creds.jev_api_key)
    try:
        with MailBox(cfg.imap_host, cfg.imap_port).login(
            creds.imap_user, creds.imap_password, initial_folder=cfg.source_folder
        ) as mb:
            try:
                recheck_expiry(mb, cfg, jev, store)
            except JevAuthError as e:
                log.error("%s - check %s in .env and your gateway credits", e, cfg.jev_api_key_env)
                return 2
            if live:
                tag_expired(mb, cfg, store)
            else:
                log.info("dry run - expiry dates saved, no tags set (use --live to tag)")
    finally:
        store.close()
    return 0
