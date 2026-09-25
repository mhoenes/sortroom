"""SQLite log of processed mails, so nothing is classified (and paid for) twice.

Also remembers when time-limited offers expire, so they can be moved to the expired folder later.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_key     TEXT PRIMARY KEY,
    processed_at    TEXT NOT NULL,
    received        TEXT,
    sender          TEXT,
    subject         TEXT,
    category        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    needs_action    REAL NOT NULL,
    moved_to        TEXT,
    flagged         INTEGER NOT NULL,
    cost_usd        REAL NOT NULL,
    expires         TEXT,                        -- last valid day of the offer (ISO date)
    expiry_checked  INTEGER NOT NULL DEFAULT 1,  -- 0 = processed before expiry tracking existed
    expired_tagged  INTEGER NOT NULL DEFAULT 0,  -- 1 = moved to the expired folder, 2 = mail no longer found
    source          TEXT NOT NULL DEFAULT 'jev', -- 'jev', 'rule' (sender rule) or 'manual' (set in the UI)
    gone            INTEGER NOT NULL DEFAULT 0   -- 1 = no longer in the mailbox (deleted), found by a reconcile
);
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started         TEXT NOT NULL,
    finished        TEXT NOT NULL,
    kind            TEXT NOT NULL,               -- run, backfill, resort, recheck
    detail          TEXT,                        -- backfill start date, re-sorted folder
    live            INTEGER NOT NULL,
    exit_code       INTEGER NOT NULL,
    classified      INTEGER NOT NULL DEFAULT 0,
    moved           INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    kept_in_inbox   INTEGER NOT NULL DEFAULT 0,
    expired_moved   INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    categories      TEXT,                        -- JSON {category: count}
    error           TEXT
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started);
"""
RUN_RETENTION_DAYS = 180

# columns added after the first release: name -> definition for ALTER TABLE
_MIGRATIONS = {
    "expires": "TEXT",
    "expiry_checked": "INTEGER NOT NULL DEFAULT 0",  # existing rows still need a check
    "expired_tagged": "INTEGER NOT NULL DEFAULT 0",
    "source": "TEXT NOT NULL DEFAULT 'jev'",
    "gone": "INTEGER NOT NULL DEFAULT 0",
}

MOVED, GONE = 1, 2


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(processed)")}
        for column, definition in _MIGRATIONS.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE processed ADD COLUMN {column} {definition}")
        # the inbox is stored as NULL; re-sorts of the inbox before 1.3.1 wrote "INBOX"
        self.db.execute("UPDATE processed SET moved_to = NULL WHERE UPPER(moved_to) = 'INBOX'")
        self.db.commit()

    def is_processed(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE message_key = ?", (key,)).fetchone() is not None

    def record(self, outcome) -> None:
        d = outcome.decision
        self.db.execute(
            """INSERT OR REPLACE INTO processed
               (message_key, processed_at, received, sender, subject, category, confidence,
                needs_action, moved_to, flagged, cost_usd, expires, expiry_checked, expired_tagged, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0,?)""",
            (
                outcome.key,
                datetime.now().isoformat(timespec="seconds"),
                outcome.received,
                outcome.sender,
                outcome.subject,
                d.category,
                d.confidence,
                d.needs_action,
                outcome.folder,
                int(outcome.flag),
                d.cost,
                outcome.expires.isoformat() if outcome.expires else None,
                getattr(outcome, "source", "jev"),
            ),
        )
        self.db.commit()

    def record_run(self, kind: str, detail: str | None, started: datetime, finished: datetime, result) -> None:
        """One line in the run log; entries older than RUN_RETENTION_DAYS are dropped."""
        self.db.execute(
            """INSERT INTO runs (started, finished, kind, detail, live, exit_code, classified, moved, flagged,
                                 kept_in_inbox, expired_moved, failed, cost_usd, categories, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (started.isoformat(timespec="seconds"), finished.isoformat(timespec="seconds"), kind, detail,
             int(result.live), result.exit_code, result.classified, result.moved, result.flagged,
             result.kept_in_inbox, result.expired_moved, result.failed, result.cost_usd,
             json.dumps(result.categories or {}, ensure_ascii=False), result.error),
        )
        cutoff = (finished - timedelta(days=RUN_RETENTION_DAYS)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM runs WHERE started < ?", (cutoff,))
        self.db.commit()

    def recent_runs(self, limit: int = 50) -> list[dict]:
        cur = self.db.execute("SELECT * FROM runs ORDER BY started DESC, id DESC LIMIT ?", (limit,))
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        for r in rows:
            r["live"] = bool(r["live"])
            r["categories"] = json.loads(r["categories"] or "{}")
        return rows

    def unchecked_expiry(self, categories: Iterable[str]) -> list[tuple[str, str | None, str | None]]:
        """(key, moved_to, received) of mails processed before expiry tracking existed."""
        cats = list(categories)
        if not cats:
            return []
        marks = ",".join("?" * len(cats))
        return self.db.execute(
            f"SELECT message_key, moved_to, received FROM processed "
            f"WHERE expiry_checked = 0 AND gone = 0 AND category IN ({marks})",
            cats,
        ).fetchall()

    def set_expiry(self, key: str, expires: date | None) -> None:
        self.db.execute(
            "UPDATE processed SET expires = ?, expiry_checked = 1 WHERE message_key = ?",
            (expires.isoformat() if expires else None, key),
        )
        self.db.commit()

    def due_expired(self, today: date) -> list[tuple[str, str | None, str | None]]:
        """(key, moved_to, received) of offers whose last valid day is before today."""
        return self.db.execute(
            "SELECT message_key, moved_to, received FROM processed "
            "WHERE expires IS NOT NULL AND expires < ? AND expired_tagged = 0 AND gone = 0",
            (today.isoformat(),),
        ).fetchall()

    def get(self, key: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM processed WHERE message_key = ?", (key,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def set_manual(self, key: str, category: str, moved_to: str | None) -> None:
        """A category set by hand in the UI: certain by definition, so it leaves the review list."""
        self.db.execute("UPDATE processed SET category = ?, moved_to = ?, confidence = 1.0, source = 'manual' "
                        "WHERE message_key = ?", (category, moved_to, key))
        self.db.commit()

    def locations(self) -> list[tuple[str, str | None, int]]:
        """(key, moved_to, gone) of every logged mail."""
        return self.db.execute("SELECT message_key, moved_to, gone FROM processed").fetchall()

    def set_gone(self, keys: Iterable[str], gone: bool) -> None:
        self.db.executemany("UPDATE processed SET gone = ? WHERE message_key = ?", [(int(gone), k) for k in keys])
        self.db.commit()

    def categories_of(self, keys: Iterable[str]) -> dict[str, str]:
        keys = list(keys)
        out: dict[str, str] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            out.update(self.db.execute(
                f"SELECT message_key, category FROM processed WHERE message_key IN ({marks})", chunk).fetchall())
        return out

    def mark_expired(self, keys: Iterable[str], state: int) -> None:
        self.db.executemany(
            "UPDATE processed SET expired_tagged = ? WHERE message_key = ?",
            [(state, k) for k in keys],
        )
        self.db.commit()

    def count_category(self, category: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM processed WHERE category = ?", (category,)).fetchone()[0]

    def rename_category(self, old: str, new: str) -> None:
        self.db.execute("UPDATE processed SET category = ? WHERE category = ?", (new, old))
        self.db.commit()

    def count_moved_to(self, folder: str) -> int:
        """Records moved to `folder` or one of its subfolders (config notation, "/" separated)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM processed WHERE moved_to = ? OR moved_to LIKE ? ESCAPE '!'",
            (folder, _like_prefix(folder)),
        ).fetchone()[0]

    def rename_moved_to(self, old: str, new: str) -> None:
        self.db.execute(
            "UPDATE processed SET moved_to = ? || substr(moved_to, ?) "
            "WHERE moved_to = ? OR moved_to LIKE ? ESCAPE '!'",
            (new, len(old) + 1, old, _like_prefix(old)),
        )
        self.db.commit()

    def misplaced(self, category: str, folder: str, min_confidence: float) -> list[tuple[str, str | None, str | None]]:
        """(key, moved_to, received) of confidently classified mails of `category` not in `folder`."""
        return self.db.execute(
            "SELECT message_key, moved_to, received FROM processed "
            "WHERE category = ? AND COALESCE(moved_to, '') != ? AND confidence >= ? AND gone = 0",
            (category, folder, min_confidence),
        ).fetchall()

    def set_moved_to(self, keys: Iterable[str], folder: str) -> None:
        self.db.executemany("UPDATE processed SET moved_to = ? WHERE message_key = ?", [(folder, k) for k in keys])
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def _like_prefix(folder: str) -> str:
    """LIKE pattern for subfolders of `folder`, with LIKE wildcards in the name escaped by '!'."""
    escaped = folder.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return escaped + "/%"
