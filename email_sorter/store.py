"""SQLite log of processed mails, so nothing is classified (and paid for) twice."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_key   TEXT PRIMARY KEY,
    processed_at  TEXT NOT NULL,
    received      TEXT,
    sender        TEXT,
    subject       TEXT,
    category      TEXT NOT NULL,
    confidence    REAL NOT NULL,
    needs_action  REAL NOT NULL,
    moved_to      TEXT,
    flagged       INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
)
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(_SCHEMA)
        self.db.commit()

    def is_processed(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE message_key = ?", (key,)).fetchone() is not None

    def record(self, outcome) -> None:
        d = outcome.decision
        self.db.execute(
            "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
