"""Read-only queries on a mailbox's state.db for the web UI."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

PAGE_SIZE = 50
PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}


def connect(workspace: Path) -> sqlite3.Connection | None:
    """The mailbox's log, opened read-only; None before its first run."""
    path = workspace / "data" / "state.db"
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# `received` is the mail's Date header with the sender's UTC offset, so plain text order is wrong
# across time zones; julianday() normalises it. Unparsable headers fall back to processed_at.
RECEIVED = "COALESCE(julianday(received), julianday(processed_at))"


def _received_since(now: datetime, span: timedelta) -> str:
    """The cutoff for `RECEIVED >= julianday(?)`, with the local offset so julianday() gets UTC right."""
    return _iso(now.astimezone() - span)


@dataclass
class MailboxStats:
    sorted_today: int = 0
    uncertain: int = 0
    flagged_7d: int = 0
    cost_month: float = 0.0
    classified_mails_month: int = 0
    last_run: dict | None = None
    errors_today: int = 0

    @property
    def cost_per_mail(self) -> float:
        return self.cost_month / self.classified_mails_month if self.classified_mails_month else 0.0


def stats(db: sqlite3.Connection | None, min_confidence: float, now: datetime | None = None) -> MailboxStats:
    s = MailboxStats()
    if db is None:
        return s
    now = now or datetime.now()
    today = _iso(datetime.combine(now.date(), datetime.min.time()))
    month = _iso(datetime.combine(now.date().replace(day=1), datetime.min.time()))
    s.sorted_today = db.execute("SELECT COUNT(*) FROM processed WHERE processed_at >= ?", (today,)).fetchone()[0]
    s.uncertain = db.execute(
        f"SELECT COUNT(*) FROM processed WHERE moved_to IS NULL AND confidence < ? AND gone = 0 "
        f"AND {RECEIVED} >= julianday(?)",
        (min_confidence, _received_since(now, timedelta(days=30)))).fetchone()[0]
    s.flagged_7d = db.execute(f"SELECT COUNT(*) FROM processed WHERE flagged = 1 AND {RECEIVED} >= julianday(?)",
                              (_received_since(now, timedelta(days=7)),)).fetchone()[0]
    cost, n = db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM processed "
        "WHERE processed_at >= ? AND source NOT IN ('rule', 'manual')",
        (month,)).fetchone()
    s.cost_month, s.classified_mails_month = cost, n
    row = db.execute("SELECT * FROM runs WHERE kind = 'run' ORDER BY started DESC, id DESC LIMIT 1").fetchone()
    s.last_run = dict(row) if row else None
    s.errors_today = db.execute("SELECT COUNT(*) FROM runs WHERE exit_code != 0 AND started >= ?",
                                (today,)).fetchone()[0]
    return s


def distribution(db: sqlite3.Connection | None, days: int = 7, now: datetime | None = None) -> list[tuple[str, int]]:
    """(category, count) of mail received in the last `days` days; '' stands for 'left in the inbox'.

    By date received, so a backfill of old mail doesn't swamp the week."""
    if db is None:
        return []
    since = _received_since(now or datetime.now(), timedelta(days=days))
    rows = db.execute(
        "SELECT CASE WHEN moved_to IS NULL THEN '' ELSE category END AS c, COUNT(*) FROM processed "
        f"WHERE {RECEIVED} >= julianday(?) GROUP BY c ORDER BY COUNT(*) DESC", (since,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def category_counts(db: sqlite3.Connection | None, days: int = 30, now: datetime | None = None) -> dict[str, int]:
    """Mails per category received in the last `days` days (moved or not)."""
    if db is None:
        return {}
    since = _received_since(now or datetime.now(), timedelta(days=days))
    return dict(db.execute(f"SELECT category, COUNT(*) FROM processed WHERE {RECEIVED} >= julianday(?) "
                           "GROUP BY category", (since,)).fetchall())


def corrections(db: sqlite3.Connection | None, min_confidence: float, days: int = 30,
                now: datetime | None = None) -> dict[str, tuple[int, int]]:
    """{category: (mails the model filed there, of those corrected by hand)} for mail received in the last
    `days` days. Mails deleted without a correction, sender rules and uncertain mails don't count."""
    if db is None:
        return {}
    since = _received_since(now or datetime.now(), timedelta(days=days))
    model = "(p.source = 'classifier' AND p.confidence >= ? AND p.gone = 0)"
    if not _has_table(db, "corrections"):
        rows = db.execute(f"SELECT p.category, COUNT(*), 0 FROM processed p WHERE {RECEIVED} >= julianday(?) "
                          f"AND {model} GROUP BY p.category", (since, min_confidence))
    else:
        rows = db.execute(
            f"SELECT COALESCE(c.category, p.category) AS cat, COUNT(*), COUNT(c.message_key) "
            f"FROM processed p LEFT JOIN corrections c ON c.message_key = p.message_key "
            f"WHERE {RECEIVED} >= julianday(?) AND (c.message_key IS NOT NULL OR {model}) GROUP BY cat",
            (since, min_confidence))
    return {cat: (n, corrected) for cat, n, corrected in rows}


def expired_moved(db: sqlite3.Connection | None, days: int = 7, now: datetime | None = None) -> int:
    if db is None:
        return 0
    since = _iso((now or datetime.now()) - timedelta(days=days))
    return db.execute("SELECT COALESCE(SUM(expired_moved), 0) FROM runs WHERE started >= ? AND live = 1",
                      (since,)).fetchone()[0]


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    """Tables added in later versions exist once a run has opened the log."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def recent_runs(db: sqlite3.Connection | None, limit: int = 8, skip_empty: bool = True) -> list[dict]:
    """Latest runs, each with `undoable`; normal runs that found nothing are left out unless they failed."""
    if db is None:
        return []
    where = "WHERE NOT (kind = 'run' AND classified = 0 AND exit_code = 0)" if skip_empty else ""
    undoable = ("live = 1 AND EXISTS (SELECT 1 FROM undo WHERE undo.run = runs.started)"
                if _has_table(db, "undo") else "0")
    return [dict(r) for r in db.execute(
        f"SELECT *, {undoable} AS undoable FROM runs {where} ORDER BY started DESC, id DESC LIMIT ?", (limit,))]


def undoable_runs(db: sqlite3.Connection | None) -> list[dict]:
    """The live runs that can be undone, newest first: started, kind, detail and the number of mails."""
    if db is None or not _has_table(db, "undo"):
        return []
    return [dict(r) for r in db.execute(
        "SELECT u.run AS started, r.kind, r.detail, COUNT(DISTINCT u.message_key) AS mails "
        "FROM undo u JOIN runs r ON r.started = u.run AND r.live = 1 GROUP BY u.run ORDER BY u.run DESC")]


def run_mails(db: sqlite3.Connection | None, run: str) -> list[dict]:
    """The mails a run sorted, newest first: their log entry now (subject etc., None where the log forgot
    the mail), where the run took them from (origin) and put them (run_moved_to), whether the run gave the
    star, and whether a later run sorted them again."""
    if db is None or not _has_table(db, "undo"):
        return []
    out = []
    for r in db.execute(
            "SELECT u.message_key AS key, u.origin, u.moved_to AS run_moved_to, u.before, "
            "EXISTS (SELECT 1 FROM undo l WHERE l.message_key = u.message_key AND l.run > u.run) AS later, "
            "p.message_key IS NOT NULL AS known, p.received, p.sender, p.subject, p.category, p.moved_to, "
            "p.flagged, p.gone, p.expired_tagged, p.source "
            f"FROM undo u LEFT JOIN processed p ON p.message_key = u.message_key WHERE u.run = ? "
            f"ORDER BY {RECEIVED} DESC", (run,)):
        m = dict(r)
        before = json.loads(m.pop("before")) if r["before"] else None
        m["starred"] = bool(m["flagged"]) and not (before and before.get("flagged"))
        out.append(m)
    return out


SENDER_DAYS = 90


def senders(db: sqlite3.Connection | None, category: str = "", now: datetime | None = None) -> list[dict]:
    """Senders with an unsubscribe link, most mails in the last SENDER_DAYS days first.

    Each: address, mails, last (received), category (the most frequent), links, one_click, unsubscribed,
    method and `since` (mails received after you unsubscribed). With `category` only mails of that
    category count; without it, senders you unsubscribed from stay listed when no mail came since.
    """
    if db is None or not _has_table(db, "senders"):
        return []
    known = {r["address"]: dict(r) for r in db.execute("SELECT * FROM senders WHERE unsubscribe IS NOT NULL")}
    rows = db.execute(
        f"SELECT lower(sender) AS address, category, received, {RECEIVED} AS jd FROM processed "
        f"WHERE {RECEIVED} >= julianday(?)" + (" AND category = ?" if category else ""),
        [_received_since(now or datetime.now(), timedelta(days=SENDER_DAYS))] + ([category] if category else []))
    stats: dict[str, dict] = {}
    for r in rows:
        if r["address"] not in known:
            continue
        s = stats.setdefault(r["address"], {"mails": 0, "jd": 0.0, "last": None, "categories": Counter()})
        s["mails"] += 1
        s["categories"][r["category"]] += 1
        if (r["jd"] or 0) >= s["jd"]:
            s["jd"], s["last"] = r["jd"] or 0, r["received"]
    out = []
    for address, info in known.items():
        got = stats.get(address)
        if got is None and (category or not info["unsubscribed"]):
            continue
        since = 0
        if info["unsubscribed"]:
            since = db.execute(f"SELECT COUNT(*) FROM processed WHERE lower(sender) = ? AND {RECEIVED} > julianday(?)",
                               (address, _iso(datetime.fromisoformat(info["unsubscribed"]).astimezone()))).fetchone()[0]
        out.append({**info, "links": json.loads(info["unsubscribe"]), "mails": got["mails"] if got else 0,
                    "last": got["last"] if got else None, "jd": got["jd"] if got else 0.0,
                    "category": got["categories"].most_common(1)[0][0] if got else None, "since": since})
    out.sort(key=lambda s: (-s["mails"], -s["jd"], s["address"]))
    return out


def uncertain_mails(db: sqlite3.Connection | None, min_confidence: float, limit: int = 10,
                    days: int | None = None, now: datetime | None = None) -> list[dict]:
    """Uncertain mails still in the inbox, newest received first; `days` limits them like stats().uncertain."""
    if db is None:
        return []
    since = f"AND {RECEIVED} >= julianday(?) " if days else ""
    args = [min_confidence] + ([_received_since(now or datetime.now(), timedelta(days=days))] if days else [])
    return [dict(r) for r in db.execute(
        f"SELECT * FROM processed WHERE moved_to IS NULL AND confidence < ? AND gone = 0 {since}"
        f"ORDER BY {RECEIVED} DESC LIMIT ?", args + [limit])]


@dataclass
class MailFilter:
    q: str = ""
    category: str = ""
    folder: str = ""      # a folder path, "inbox", "gone" or ""
    period: str = "7d"
    uncertain: bool = False
    flagged: bool = False
    show_gone: bool = False  # deleted mails are hidden unless asked for
    page: int = 1


def mails(db: sqlite3.Connection | None, f: MailFilter, min_confidence: float,
          now: datetime | None = None) -> tuple[list[dict], int]:
    """One page of processed mail matching the filter, newest received first, and the total count."""
    if db is None:
        return [], 0
    where: list[str] = []
    args: list[object] = []
    span = PERIODS.get(f.period)
    if span:
        where.append(f"{RECEIVED} >= julianday(?)")
        args.append(_received_since(now or datetime.now(), span))
    if f.q:
        where.append("(sender LIKE ? ESCAPE '!' OR subject LIKE ? ESCAPE '!')")
        like = "%" + f.q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        args += [like, like]
    if f.category:
        where.append("category = ?")
        args.append(f.category)
    if f.folder == "inbox":
        where.append("moved_to IS NULL")
    elif f.folder:
        where.append("moved_to = ?")
        args.append(f.folder)
    if f.uncertain:
        where.append("moved_to IS NULL AND confidence < ?")
        args.append(min_confidence)
    if f.flagged:
        where.append("flagged = 1")
    if not f.show_gone:
        where.append("gone = 0")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM processed {sql_where}", args).fetchone()[0]
    offset = (max(f.page, 1) - 1) * PAGE_SIZE
    rows = db.execute(f"SELECT * FROM processed {sql_where} ORDER BY {RECEIVED} DESC, processed_at DESC "
                      f"LIMIT ? OFFSET ?", args + [PAGE_SIZE, offset]).fetchall()
    return [dict(r) for r in rows], total


def mail(db: sqlite3.Connection | None, key: str) -> dict | None:
    if db is None or not key:
        return None
    row = db.execute("SELECT * FROM processed WHERE message_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def folders(db: sqlite3.Connection | None) -> list[str]:
    if db is None:
        return []
    return [r[0] for r in db.execute("SELECT DISTINCT moved_to FROM processed "
                                     "WHERE moved_to IS NOT NULL AND UPPER(moved_to) != 'INBOX' ORDER BY 1")]


def month_start(today: date | None = None) -> date:
    return (today or date.today()).replace(day=1)
