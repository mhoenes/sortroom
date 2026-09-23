"""SQLite log of processed mails, so nothing is classified (and paid for) twice.

Also remembers when time-limited offers expire, so they can be tagged later.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
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
    expired_tagged  INTEGER NOT NULL DEFAULT 0   -- 1 = tagged, 2 = mail no longer found
)
"""

# columns added after the first release: name -> definition for ALTER TABLE
_MIGRATIONS = {
    "expires": "TEXT",
    "expiry_checked": "INTEGER NOT NULL DEFAULT 0",  # existing rows still need a check
    "expired_tagged": "INTEGER NOT NULL DEFAULT 0",
}

TAGGED, GONE = 1, 2


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(_SCHEMA)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(processed)")}
        for column, definition in _MIGRATIONS.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE processed ADD COLUMN {column} {definition}")
        self.db.commit()

    def is_processed(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE message_key = ?", (key,)).fetchone() is not None

    def record(self, outcome) -> None:
        d = outcome.decision
        self.db.execute(
            """INSERT OR REPLACE INTO processed
               (message_key, processed_at, received, sender, subject, category, confidence,
                needs_action, moved_to, flagged, cost_usd, expires, expiry_checked, expired_tagged)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0)""",
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
            ),
        )
        self.db.commit()

    def unchecked_expiry(self, categories: Iterable[str]) -> list[tuple[str, str | None, str | None]]:
        """(key, moved_to, received) of mails processed before expiry tracking existed."""
        cats = list(categories)
        if not cats:
            return []
        marks = ",".join("?" * len(cats))
        return self.db.execute(
            f"SELECT message_key, moved_to, received FROM processed "
            f"WHERE expiry_checked = 0 AND category IN ({marks})",
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
            "WHERE expires IS NOT NULL AND expires < ? AND expired_tagged = 0",
            (today.isoformat(),),
        ).fetchall()

    def mark_tagged(self, keys: Iterable[str], state: int) -> None:
        self.db.executemany(
            "UPDATE processed SET expired_tagged = ? WHERE message_key = ?",
            [(state, k) for k in keys],
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
