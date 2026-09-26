"""Read-only queries on a mailbox's state.db for the web UI."""
from __future__ import annotations

import sqlite3
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
        "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM processed WHERE processed_at >= ? AND source NOT IN ('rule', 'manual')",
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


def expired_moved(db: sqlite3.Connection | None, days: int = 7, now: datetime | None = None) -> int:
    if db is None:
        return 0
    since = _iso((now or datetime.now()) - timedelta(days=days))
    return db.execute("SELECT COALESCE(SUM(expired_moved), 0) FROM runs WHERE started >= ? AND live = 1",
                      (since,)).fetchone()[0]


def recent_runs(db: sqlite3.Connection | None, limit: int = 8, skip_empty: bool = True) -> list[dict]:
    """Latest runs; normal runs that found nothing are left out unless they failed."""
    if db is None:
        return []
    where = "WHERE NOT (kind = 'run' AND classified = 0 AND exit_code = 0)" if skip_empty else ""
    return [dict(r) for r in db.execute(f"SELECT * FROM runs {where} ORDER BY started DESC, id DESC LIMIT ?",
                                        (limit,))]


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
    where, args = [], []
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
