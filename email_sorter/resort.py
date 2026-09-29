"""Re-sort one folder after categories changed:  python -m email_sorter --resort-folder INBOX/Reisen [--live]

Every mail in the folder is classified again. A mail is moved only when the model puts it
confidently into a category with a *different* folder; uncertain mails and mails whose
category has no folder (they would belong in the inbox) stay where they are.
Flags are not touched. The log gets the new category, folder and expiry date.
"""
from __future__ import annotations

from dataclasses import replace

import logging
from collections import Counter, defaultdict
from pathlib import Path

from imap_tools import AND, MailBox

from .config import Config, Credentials
from .classifier import ClassifierAuthError, ClassifierClient, ClassifierError
from .mailtext import build_state, message_key
from datetime import datetime

from .config import INBOX_ACTION
from .sorter import (IMAP_TIMEOUT, Outcome, ReportWriter, RunResult, UID_CHUNK, expiry_for, move_uids,
                     plan, ready_to_sort, rule_outcome, server_folder, _auth_failed, _chunks,
                     _delimiter, _ensure_folder, _finish)
from .store import Store
from .oauth import sign_in

log = logging.getLogger(__name__)


def resort_outcomes(mb: MailBox, cfg: Config, classifier: ClassifierClient, folder: str, limit: int | None,
                    on_outcome=None, min_age_hours: float = 0) -> tuple[list[Outcome], list[str], list[Outcome]]:
    """Classify the mails of the selected `folder` (config notation).

    Returns (all outcomes, failed keys, outcomes to move). Outcome.folder is the folder the
    mail will be in afterwards - the same folder for mails that stay.
    """
    uids = sorted(mb.uids(), key=int, reverse=True)  # newest first
    uids = ready_to_sort(mb, uids, cfg, min_age_hours)  # re-sorting the inbox: fresh mail waits like in runs
    if limit:
        uids = uids[:limit]
    log.info("re-sorting %d mail(s) in %s", len(uids), folder)
    outcomes: list[Outcome] = []
    moving: list[Outcome] = []
    failed: list[str] = []
    seen: set[str] = set()
    kept = 0
    for chunk in _chunks(uids):
        wanted = set(chunk)
        for msg in mb.fetch(AND(uid=chunk), mark_seen=False, bulk=UID_CHUNK):
            if msg.uid not in wanted or msg.uid in seen:
                continue  # unsolicited FETCH response
            seen.add(msg.uid)
            rule = cfg.rule_for(msg.from_)
            if rule and rule.action == INBOX_ACTION:
                kept += 1
                continue
            key = message_key(msg)
            if rule:  # sender rule: its category's folder, no model request
                target = cfg.categories[rule.action].folder
                outcome = rule_outcome(cfg, rule, msg.uid, key, msg,
                                       where=target if target and target != folder else folder)
                outcomes.append(outcome)
                if outcome.folder != folder:
                    moving.append(outcome)
                if on_outcome:
                    on_outcome(outcome)
                continue
            try:
                decision = classifier.decide(build_state(msg, cfg.max_body_chars), cfg.descriptions)
            except ClassifierAuthError:
                raise
            except ClassifierError as e:
                failed.append(key)
                log.warning("could not classify %r: %s", msg.subject, e)
                continue
            target, _flag, _expiry = plan(decision, cfg)
            if target is None:
                where, note = folder, ("uncertain, stays" if decision.confidence < cfg.min_confidence
                                       else "category belongs in the inbox, stays")
            elif target == folder:
                where, note = folder, ""
            else:
                where, note = target, f"moves from {folder}"
            outcome = Outcome(
                key=key, uid=msg.uid,
                received=msg.date.isoformat(timespec="minutes") if msg.date else msg.date_str,
                sender=msg.from_, subject=msg.subject, decision=decision,
                folder=where, flag=False, note=note, expires=expiry_for(decision, cfg, msg),
            )
            outcomes.append(outcome)
            if where != folder:
                moving.append(outcome)
            if on_outcome:
                on_outcome(outcome)
    if kept:
        log.info("%d mail(s) left alone by inbox sender rules", kept)
    missing = len(uids) - len(seen)
    if missing:
        log.warning("%d mail(s) were not returned by the server", missing)
    return outcomes, failed, moving


def run_resort(cfg: Config, creds: Credentials, base_dir: Path, folder: str, live: bool,
               limit: int | None) -> RunResult:
    store = Store(base_dir / "data" / "state.db")
    classifier = cfg.classifier_client(creds.classifier_api_key)
    report = None if live else ReportWriter(base_dir / "reports")
    started = datetime.now()
    try:
        with sign_in(MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT), creds, cfg.source_folder) as mb:
            delim = _delimiter(mb)
            # accept "INBOX/Reisen" (config notation) as well as "INBOX.Reisen" (server notation)
            folder = "/".join(p for p in folder.replace(delim, "/").split("/") if p)
            if cfg.expired_folder and folder == cfg.expired_folder.strip("/"):
                log.error("%s holds expired offers; re-sorting it would move them back out", folder)
                return RunResult(exit_code=2, error=f"{folder} is the expired-offers folder")
            srv = server_folder(folder, delim)
            if not mb.folder.exists(srv):
                log.error("folder %s does not exist", srv)
                return RunResult(exit_code=2, error=f"folder {srv} not found")
            mb.folder.set(srv)
            try:
                min_age = cfg.min_age_hours if folder == cfg.source_folder.strip("/") else 0
                outcomes, failed, moving = resort_outcomes(mb, cfg, classifier, folder, limit,
                                                           on_outcome=report.write if report else None,
                                                           min_age_hours=min_age)
            except ClassifierAuthError as e:
                return _finish(store, "resort", folder, started, _auth_failed(cfg, e))

            move_failures: set[str] = set()
            if live:
                by_target: dict[str, list[Outcome]] = defaultdict(list)
                for o in moving:
                    by_target[server_folder(o.folder, delim)].append(o)
                for target, group in by_target.items():
                    try:
                        _ensure_folder(mb, target)
                        for chunk in _chunks([o.uid for o in group]):
                            move_uids(mb, chunk, target)
                        log.info("moved %d mail(s) %s -> %s", len(group), srv, target)
                    except Exception as e:
                        log.error("moving to %s failed: %s", target, e)
                        move_failures.update(o.key for o in group)
                source = cfg.source_folder.strip("/")
                for o in outcomes:
                    if o.key in move_failures:
                        continue
                    store.record(replace(o, folder=None) if (o.folder or "").strip("/") == source else o)
            elif outcomes:
                log.info("dry run - nothing changed. Report: %s", report.path)
            mb.folder.set(cfg.source_folder)

            moved = len(moving) - (len(move_failures) if live else 0)
            targets = Counter(o.folder for o in moving if o.key not in move_failures)
            log.info("%s %d mail(s) out of %s, %d stay%s", "moved" if live else "would move", moved, folder,
                     len(outcomes) - len(moving),
                     ": " + ", ".join(f"{t}={n}" for t, n in targets.most_common()) if targets else "")
            return _finish(store, "resort", folder, started, RunResult(
                exit_code=1 if (failed or move_failures) else 0,
                live=live,
                classified=len(outcomes),
                moved=moved,
                time_limited_offers=sum(1 for o in outcomes if o.expires),
                failed=len(failed) + len(move_failures),
                cost_usd=round(sum(o.decision.cost for o in outcomes), 6),
                categories=dict(Counter(o.decision.category for o in outcomes).most_common()),
                report=str(report.path) if report and report.rows else None,
            ))
    except Exception as e:
        _finish(store, "resort", folder, started, RunResult(exit_code=1, live=live, error=str(e)))
        raise
    finally:
        store.close()
        if report:
            report.close()
