"""Sorting: find new mails, ask the classifier, then move and star them (live) or write a report (dry run) -
the normal run, the backfill and the expired offers. The IMAP helpers are in imap.py."""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Callable

from imap_tools import AND, MailBox, MailMessage, MailMessageFlags

from .config import INBOX_ACTION, Config, Credentials, SenderRule
from .expiry import resolve_expiry
from .classifier import Decision, ClassifierAuthError, ClassifierClient, ClassifierError, ClassifierOutage
from .mailtext import build_state, full_text, message_key, received_of, sender_name, sent_date, unsubscribe_links
from .store import GONE, MOVED, Store
from .imap import (BODY_CHUNK, UID_CHUNK, chunks, connect, delimiter, ensure_folder, find_uids, group_by_folder,
                   move_uids, received_times, seen_uids, server_folder)

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
    source: str = "classifier"   # "classifier" or "rule" (sender rule, no model request)
    unsubscribe: tuple[list[str], bool] | None = None  # List-Unsubscribe links, one-click offered
    sender_name: str = ""        # the sender's display name, empty = none


def rule_outcome(cfg: Config, rule: SenderRule, uid: str, key: str, msg: MailMessage,
                 where: str | None = None) -> Outcome:
    """A mail placed by a sender rule: its category's folder (or `where`), no flag, no model cost."""
    decision = Decision(rule.action, 1.0, {rule.action: 1.0}, 0.0, 0.0)
    folder = cfg.categories[rule.action].folder if where is None else where
    return Outcome(key=key, uid=uid, received=received_of(msg),
                   sender=msg.from_, subject=msg.subject, decision=decision, folder=folder, flag=False,
                   note=f"sender rule: {rule.match}", source="rule", unsubscribe=unsubscribe_links(msg),
                   sender_name=sender_name(msg))


def plan(decision: Decision, cfg: Config) -> tuple[str | None, bool, str]:
    """Decide what to do with a mail: (target folder, flag?, note)."""
    category = cfg.categories[decision.category]
    confident = decision.confidence >= cfg.min_confidence
    folder = category.folder if confident else None
    needs_action = category.flag_on_action and decision.needs_action >= cfg.action_flag_threshold
    flag = (category.flag and confident) or needs_action
    note = "" if confident else f"low confidence (< {cfg.min_confidence:.2f}), stays in inbox"
    return folder, flag, note


def ready_to_sort(mb: MailBox, uids: list[str], cfg: Config, min_age_hours: float) -> list[str]:
    """The UIDs past the waiting time; with sort_read_at_once also younger ones already read."""
    if min_age_hours <= 0 or not uids:
        return uids
    ready = set(old_enough(received_times(mb, uids), uids, min_age_hours))
    waiting = [u for u in uids if u not in ready]
    if waiting and cfg.sort_read_at_once:
        read = seen_uids(mb, waiting)
        if read:
            log.info("%d mail(s) younger than %gh already read, sorted now", len(read), min_age_hours)
        ready |= read
    if len(ready) < len(uids):
        log.info("%d mail(s) younger than %gh, left for a later run", len(uids) - len(ready), min_age_hours)
    return [u for u in uids if u in ready]


def old_enough(times: dict[str, datetime], uids: list[str], min_age_hours: float,
               now: datetime | None = None) -> list[str]:
    """UIDs that arrived at least min_age_hours ago. Unknown arrival time counts as old enough."""
    if min_age_hours <= 0:
        return uids
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=min_age_hours)
    return [u for u in uids if u not in times or times[u] <= cutoff]


def expiry_for(decision: Decision, cfg: Config, msg: MailMessage) -> date | None:
    """End date of a time-limited offer, for categories that track it. A mail whose date can't be
    worked out keeps its (paid) decision, just without an expiry date."""
    if not cfg.categories[decision.category].track_expiry or decision.has_expiry < cfg.expiry_threshold:
        return None
    try:
        return resolve_expiry(full_text(msg), sent_date(msg), decision.expiry_window)
    except Exception:
        log.exception("could not work out the expiry date of %r", msg.subject)
        return None


def mail_failed(msg: MailMessage, e: Exception, what: str = "classify") -> None:
    """Log a mail that failed on its own. The run goes on; the mail is tried again next time."""
    if isinstance(e, ClassifierError):
        log.warning("could not %s %r: %s", what, msg.subject, e)
    else:  # a bug or a mail we can't read: keep the traceback in the log file
        log.exception("could not %s %r (%s)", what, msg.subject, type(e).__name__)


def classify_new(
    mb: MailBox,
    cfg: Config,
    classifier: ClassifierClient,
    store: Store,
    limit: int,
    since: date,
    before: date | None = None,
    exclude: set[str] | None = None,
    on_outcome: Callable[[Outcome], None] | None = None,
) -> tuple[list[Outcome], list[str], list[str]]:
    """Classify unprocessed mail received in [since, before).

    Returns (outcomes, failed keys, attempted keys). Attempted covers every mail picked
    for this batch, including ones the server did not return, so callers can make progress.
    """
    criteria = AND(date_gte=since, date_lt=before) if before else AND(date_gte=since)
    pending: dict[str, str] = {}  # uid -> message key
    by_rule: dict[str, tuple[SenderRule, MailMessage]] = {}  # uid -> category rule and headers
    kept = 0
    for head in mb.fetch(criteria, mark_seen=False, headers_only=True, bulk=UID_CHUNK):
        key = message_key(head)
        if not head.uid or store.is_processed(key) or (exclude and key in exclude):
            continue
        rule = cfg.rule_for(head.from_)
        if rule and rule.action == INBOX_ACTION:
            kept += 1
            continue
        pending[head.uid] = key
        if rule:
            by_rule[head.uid] = (rule, head)
    if kept:
        log.info("%d mail(s) left in the inbox by sender rules", kept)

    if cfg.min_age_hours > 0 and pending:
        pending = {u: pending[u] for u in ready_to_sort(mb, list(pending), cfg, cfg.min_age_hours)}

    # sender rules need no model request, so they don't count against the limit
    ruled = sorted((u for u in pending if u in by_rule), key=int, reverse=True)
    uids = sorted((u for u in pending if u not in by_rule), key=int, reverse=True)[:limit]  # newest first
    span = f"{since} to {before - timedelta(days=1)}" if before else f"since {since}"
    log.info("%d new mail(s) in %s %s, classifying %d%s", len(pending), cfg.source_folder, span, len(uids),
             f", {len(ruled)} by sender rule" if ruled else "")
    if not uids and not ruled:
        return [], [], []

    outcomes: list[Outcome] = []
    for uid in ruled:
        rule, head = by_rule[uid]
        outcome = rule_outcome(cfg, rule, uid, pending[uid], head)
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
    failed: list[str] = []
    attempted = [pending[u] for u in ruled + uids]
    if not uids:
        return outcomes, failed, attempted
    uid_by_key = {pending[u]: u for u in uids}
    done: set[str] = set()
    descriptions = cfg.descriptions
    for msg in mb.fetch(AND(uid=uids), mark_seen=False, bulk=BODY_CHUNK):
        # match by UID; fall back to the message key when the server's response carries
        # no usable UID (e.g. another client changed flags while we were fetching)
        matched = msg.uid if msg.uid in pending else uid_by_key.get(message_key(msg)) if msg.headers else None
        if matched is None or pending[matched] in done:
            continue  # unsolicited FETCH without a mail we asked for
        done.add(pending[matched])
        msg_uid = matched
        try:  # whatever goes wrong with one mail, the others are still sorted
            decision = classifier.decide(build_state(msg, cfg.max_body_chars), descriptions)
            folder, flag, note = plan(decision, cfg)
            outcome = Outcome(
                key=pending[msg_uid],
                uid=msg_uid,
                received=received_of(msg),
                sender=msg.from_,
                subject=msg.subject,
                decision=decision,
                folder=folder,
                flag=flag,
                note=note,
                expires=expiry_for(decision, cfg, msg),
                unsubscribe=unsubscribe_links(msg),
                sender_name=sender_name(msg),
            )
        except ClassifierAuthError:
            raise
        except ClassifierOutage:
            failed.append(pending[msg_uid])
            break  # what was classified is still applied
        except Exception as e:
            failed.append(pending[msg_uid])
            mail_failed(msg, e)
            continue
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        log.debug("%.2f %-18s %s", decision.confidence, decision.category, msg.subject)
    missing = [pending[u] for u in uids if pending[u] not in done]
    if missing:
        if outage(classifier):
            log.warning("%d mail(s) left for the next run", len(missing))
        else:
            log.warning("%d mail(s) were not returned by the server, retried next run", len(missing))
        failed.extend(missing)
    return outcomes, failed, attempted


def outage(classifier) -> str | None:
    """Why the classifier stopped for this run, if it did (see ClassifierOutage)."""
    return getattr(classifier, "outage", None)


def with_outage(result: RunResult, classifier) -> RunResult:
    """Mark a run that stopped because the classifier was down: it shows as aborted, with the reason."""
    reason = outage(classifier)
    if reason:
        result.exit_code, result.error = max(result.exit_code, 1), reason
    return result


def apply(mb: MailBox, outcomes: list[Outcome], store: Store) -> int:
    """Flag and move mails on the server; record what succeeded. Returns failure count."""
    delim = delimiter(mb)
    failures = 0

    to_flag = [o.uid for o in outcomes if o.flag]
    if to_flag:
        try:
            for chunk in chunks(to_flag):
                mb.flag(chunk, MailMessageFlags.FLAGGED, True)
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
            ensure_folder(mb, folder)
            for chunk in chunks([o.uid for o in group]):
                move_uids(mb, chunk, folder)
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


def expired_target(cfg: Config, category: str | None) -> str | None:
    """Where an expired offer of this category goes: the category's own folder, else the default."""
    cat = cfg.categories.get(category or "")
    return (cat.expired_folder if cat and cat.expired_folder else None) or cfg.expired_folder


def move_expired(mb: MailBox, cfg: Config, store: Store, today: date | None = None) -> int:
    """Move offers whose last valid day has passed to their expired folder. Returns the number moved.

    Offers of a category without any expired folder stay where they are; their dates stay in the
    log until a folder is configured.
    """
    if not cfg.expired_folder and not any(c.expired_folder for c in cfg.categories.values()):
        return 0
    due = store.due_expired(today or date.today())
    if not due:
        return 0
    categories = store.categories_of(k for k, _, _ in due)
    by_target: dict[str, list] = defaultdict(list)
    for row in due:
        target = expired_target(cfg, categories.get(row[0]))
        if target:
            by_target[target].append(row)
    delim = delimiter(mb)
    moved = 0
    try:
        for target_path, rows in by_target.items():
            target = server_folder(target_path, delim)
            for folder, wanted in group_by_folder(rows, cfg, delim).items():
                try:
                    uids = find_uids(mb, folder, wanted, fallback_days=MAX_EXPIRY_AGE_DAYS)
                    if uids and target != folder:
                        ensure_folder(mb, target)
                        for chunk in chunks(list(uids.values())):
                            move_uids(mb, chunk, target)
                        log.info("moved %d expired offer(s) %s -> %s", len(uids), folder, target)
                    store.mark_expired(uids, MOVED)
                    store.mark_expired(set(wanted) - set(uids), GONE)  # deleted or moved away by the user
                    moved += len(uids)
                except Exception as e:
                    log.error("moving expired offers out of %s failed, will retry next run: %s", folder, e)
    finally:
        mb.folder.set(cfg.source_folder)
    return moved


def recheck_expiry(mb: MailBox, cfg: Config, classifier: ClassifierClient, store: Store) -> int:
    """One-off: work out expiry dates for mails sorted before expiry tracking existed."""
    tracked = [k for k, c in cfg.categories.items() if c.track_expiry]
    todo = store.unchecked_expiry(tracked)
    log.info("%d already sorted mail(s) to check for an expiry date", len(todo))
    delim = delimiter(mb)
    found = 0
    try:
        for folder, wanted in group_by_folder(todo, cfg, delim).items():
            uids = find_uids(mb, folder, wanted, fallback_days=cfg.lookback_days + 1)
            for key in set(wanted) - set(uids):
                store.set_expiry(key, None)  # no longer there
            key_by_uid = {uid: key for key, uid in uids.items()}
            if not key_by_uid:
                continue
            messages = (msg for chunk in chunks(list(key_by_uid))
                        for msg in mb.fetch(AND(uid=chunk), mark_seen=False, bulk=BODY_CHUNK))
            for msg in messages:
                if msg.uid not in key_by_uid:
                    continue  # unsolicited FETCH from the server
                try:
                    decision = classifier.decide(build_state(msg, cfg.max_body_chars), cfg.descriptions)
                    # the category was decided earlier; only the expiry answers matter here
                    expires = None
                    if decision.has_expiry >= cfg.expiry_threshold:
                        expires = resolve_expiry(full_text(msg), sent_date(msg), decision.expiry_window)
                except ClassifierAuthError:
                    raise
                except ClassifierOutage:
                    break  # the rest stays unchecked, retried next time
                except Exception as e:
                    mail_failed(msg, e, "check")
                    continue  # stays unchecked, retried next time
                store.set_expiry(key_by_uid[msg.uid], expires)
                if expires:
                    found += 1
                    log.debug("expires %s: %s", expires, msg.subject)
            if outage(classifier):
                break
    finally:
        mb.folder.set(cfg.source_folder)
    log.info("found %d time-limited offer(s) among already sorted mail", found)
    return found


@dataclass
class RunResult:
    """Outcome of a run: exit code for the CLI, summary for the HTTP API."""
    exit_code: int
    live: bool = False
    classified: int = 0
    moved: int = 0
    flagged: int = 0
    kept_in_inbox: int = 0
    time_limited_offers: int = 0
    expired_moved: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    categories: dict[str, int] | None = None
    report: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.exit_code == 0, **self.__dict__, "categories": self.categories or {}}


def summarize(outcomes: list[Outcome], live: bool, exit_code: int, failed: int = 0,
              expired_moved: int = 0, report: Path | None = None) -> RunResult:
    counts = Counter(o.decision.category for o in outcomes)
    moved = sum(1 for o in outcomes if o.folder)
    result = RunResult(
        exit_code=exit_code,
        live=live,
        classified=len(outcomes),
        moved=moved,
        flagged=sum(1 for o in outcomes if o.flag),
        kept_in_inbox=len(outcomes) - moved,
        time_limited_offers=sum(1 for o in outcomes if o.expires),
        expired_moved=expired_moved,
        failed=failed,
        cost_usd=round(sum(o.decision.cost for o in outcomes), 6),
        categories=dict(counts.most_common()),
        report=str(report) if report else None,
    )
    if outcomes:
        log.info("categories: %s", ", ".join(f"{k}={v}" for k, v in counts.most_common()))
        log.info("%s %d, flagged %d, kept %d in inbox, %d time-limited offer(s), cost $%.6f",
                 "moved" if live else "would move", result.moved, result.flagged,
                 result.kept_in_inbox, result.time_limited_offers, result.cost_usd)
    return result


def finish(store: Store, kind: str, detail: str | None, started: datetime, result: RunResult) -> RunResult:
    """Write the result to the mailbox's run log; logging must never break a run."""
    try:
        store.record_run(kind, detail, started, datetime.now(), result)
    except Exception:
        log.exception("could not write the run log")
    return result


def auth_failed(cfg: Config, e: Exception) -> RunResult:
    log.error("%s - check the API key under Global settings and your credit with the provider", e)
    return RunResult(exit_code=2, error=str(e))


def run(cfg: Config, creds: Credentials, base_dir: Path, live: bool, limit: int | None) -> RunResult:
    """Normal run: classify new mail from the last lookback_days."""
    store = Store(base_dir / "data" / "state.db")
    classifier = cfg.classifier_client(creds.classifier_api_key)
    limit = min(limit or cfg.max_per_run, cfg.max_per_run)
    report = None if live else ReportWriter(base_dir / "reports")
    started = datetime.now()
    if live:
        store.track(started)
    try:
        with connect(cfg, creds) as mb:
            since = date.today() - timedelta(days=cfg.lookback_days)
            try:
                outcomes, failed, _moving = classify_new(mb, cfg, classifier, store, limit, since,
                                                   on_outcome=report.write if report else None)
            except ClassifierAuthError as e:
                return finish(store, "run", None, started, auth_failed(cfg, e))
            failures = tagged = 0
            if live:
                failures = apply(mb, outcomes, store)
                tagged = move_expired(mb, cfg, store)
            elif outcomes and report:
                log.info("dry run - nothing changed. Report: %s", report.path)
            code = 1 if (failed or failures) else 0
            return finish(store, "run", None, started, with_outage(
                summarize(outcomes, live, code, failed=len(failed) + failures, expired_moved=tagged,
                          report=report.path if report and report.rows else None), classifier))
    except Exception as e:
        finish(store, "run", None, started, RunResult(exit_code=1, live=live, error=str(e)))
        raise
    finally:
        store.close()
        if report:
            report.close()


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
                 since: date, limit: int | None) -> RunResult:
    """--since: manually sort older mail, month by month (newest first) in batches.

    Only reachable from the command line; the scheduled task never passes --since.
    Live batches are applied and recorded right away, so an interrupted backfill
    resumes where it stopped.
    """
    store = Store(base_dir / "data" / "state.db")
    classifier = cfg.classifier_client(creds.classifier_api_key)
    report = None if live else ReportWriter(base_dir / "reports")
    remaining = limit
    all_outcomes: list[Outcome] = []
    all_failed: list[str] = []
    failures = 0
    seen: set[str] = set()  # dry run records nothing; don't classify the same mail twice
    started, detail = datetime.now(), f"since {since}"
    if live:
        store.track(started)
    try:
        with connect(cfg, creds) as mb:
            for start, end in month_windows(since, date.today() + timedelta(days=1)):
                while remaining is None or remaining > 0:
                    batch = min(cfg.max_per_run, remaining) if remaining is not None else cfg.max_per_run
                    try:
                        outcomes, failed, attempted = classify_new(
                            mb, cfg, classifier, store, batch, start, before=end, exclude=seen,
                            on_outcome=report.write if report else None,
                        )
                    except ClassifierAuthError as e:
                        return finish(store, "backfill", detail, started, auth_failed(cfg, e))
                    if not attempted:
                        break  # month done
                    seen.update(attempted)  # failed ones are retried on the next backfill, not in this one
                    if live and outcomes:
                        failures += apply(mb, outcomes, store)
                    all_outcomes.extend(outcomes)
                    all_failed.extend(failed)
                    if remaining is not None:
                        remaining -= len(attempted)
                    if outage(classifier):
                        break
                if outage(classifier):
                    break
                if remaining is not None and remaining <= 0:
                    log.info("limit reached, stopping")
                    break
            tagged = 0
            if live:
                tagged = move_expired(mb, cfg, store)
            elif all_outcomes and report:
                log.info("dry run - nothing changed. Report: %s", report.path)
            if all_failed:
                log.warning("%d mail(s) could not be classified; run the backfill again to retry", len(all_failed))
            code = 1 if (all_failed or failures) else 0
            return finish(store, "backfill", detail, started, with_outage(
                summarize(all_outcomes, live, code, failed=len(all_failed) + failures,
                          expired_moved=tagged, report=report.path if report and report.rows else None), classifier))
    except Exception as e:
        finish(store, "backfill", detail, started, RunResult(exit_code=1, live=live, error=str(e)))
        raise
    finally:
        store.close()
        if report:
            report.close()


def run_recheck_expiry(cfg: Config, creds: Credentials, base_dir: Path, live: bool) -> RunResult:
    """--recheck-expiry: find expiry dates for already sorted mail, then move expired ones (live only)."""
    store = Store(base_dir / "data" / "state.db")
    classifier = cfg.classifier_client(creds.classifier_api_key)
    started = datetime.now()
    try:
        with connect(cfg, creds) as mb:
            try:
                found = recheck_expiry(mb, cfg, classifier, store)
            except ClassifierAuthError as e:
                return finish(store, "recheck", None, started, auth_failed(cfg, e))
            tagged = 0
            if live:
                tagged = move_expired(mb, cfg, store)
            else:
                log.info("dry run - expiry dates saved, nothing moved (use --live to move expired offers)")
            return finish(store, "recheck", None, started, with_outage(
                RunResult(exit_code=0, live=live, time_limited_offers=found, expired_moved=tagged), classifier))
    except Exception as e:
        finish(store, "recheck", None, started, RunResult(exit_code=1, live=live, error=str(e)))
        raise
    finally:
        store.close()
