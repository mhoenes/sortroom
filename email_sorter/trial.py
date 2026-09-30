"""Try a changed category description on real, already sorted mail before saving it.

Read-only: mails are fetched without marking them seen, nothing is moved and nothing is written to
the log. The sample is the category's most recent mails plus recent mails of other categories, so
the result shows both what the category keeps and what it would newly pull in.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from imap_tools import AND, MailBox

from .config import Category, Config, Credentials
from .classifier import ClassifierAuthError, ClassifierError
from .i18n import _
from .mailtext import build_state, message_key
from .sorter import BODY_CHUNK, IMAP_TIMEOUT, MAX_EXPIRY_AGE_DAYS, _delimiter, _find_uids, _group_by_folder
from .oauth import sign_in

log = logging.getLogger(__name__)

OWN_SAMPLE = 10
OTHER_SAMPLE = 15


@dataclass
class TrialRow:
    received: str | None
    sender: str
    subject: str
    before: str
    after: str | None = None
    confidence: float | None = None
    error: str | None = None


def sample(db_path: Path, category: str, own: int = OWN_SAMPLE, other: int = OTHER_SAMPLE) -> list[tuple]:
    """(key, moved_to, received, sender, subject, category) of mails decided by the model, newest first."""
    if not db_path.exists():
        return []
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(processed)")}
        classified_only = "AND source NOT IN ('rule', 'manual')" if "source" in cols else ""
        sql = ("SELECT message_key, moved_to, received, sender, subject, category FROM processed "
               f"WHERE category {{}} ? {classified_only} ORDER BY processed_at DESC LIMIT ?")
        return (db.execute(sql.format("="), (category, own)).fetchall()
                + db.execute(sql.format("!="), (category, other)).fetchall())
    finally:
        db.close()


def with_category(cfg: Config, key: str, description: str) -> dict[str, str]:
    """The descriptions the model sees, with one category changed or added."""
    cats = dict(cfg.categories)
    cats[key] = replace(cats[key], description=description) if key in cats else Category(
        key, description, None, False, True, False)
    return {k: c.description for k, c in cats.items()}


def run_trial(cfg: Config, creds: Credentials, db_path: Path, key: str, description: str,
              progress=None) -> tuple[list[TrialRow], float]:
    """Classify the sample with the draft description. Returns (rows, cost in USD)."""
    picked = sample(db_path, key)
    rows = {k: TrialRow(received, sender or "", subject or "", category)
            for k, _, received, sender, subject, category in picked}
    if not rows:
        return [], 0.0
    descriptions = with_category(cfg, key, description)
    classifier = cfg.classifier_client(creds.classifier_api_key)
    cost, done = 0.0, 0
    with sign_in(MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT), creds, cfg.source_folder) as mb:
        delim = _delimiter(mb)
        for folder, wanted in _group_by_folder([(k, m, r) for k, m, r, *_ in picked], cfg, delim).items():
            try:
                uids = _find_uids(mb, folder, wanted, fallback_days=MAX_EXPIRY_AGE_DAYS)
            except Exception as e:
                log.warning("trial: cannot read %s: %s", folder, e)
                uids = {}
            for k in set(wanted) - set(uids):
                rows[k].error = _("no longer in the folder")
            if not uids:
                continue
            for msg in mb.fetch(AND(uid=list(uids.values())), mark_seen=False, bulk=BODY_CHUNK):
                k = message_key(msg)
                if k not in rows or rows[k].after or rows[k].error:
                    continue
                try:
                    d = classifier.decide(build_state(msg, cfg.max_body_chars), descriptions)
                except ClassifierAuthError:
                    raise
                except ClassifierError as e:
                    rows[k].error = str(e)[:120]
                    continue
                rows[k].after, rows[k].confidence = d.category, d.confidence
                cost += d.cost
                done += 1
                if progress:
                    progress(done, len(rows))
    for r in rows.values():
        if not r.after and not r.error:
            r.error = _("not delivered by the server")
    return list(rows.values()), cost
