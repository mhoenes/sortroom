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
    source          TEXT NOT NULL DEFAULT 'classifier', -- 'classifier', 'rule' (sender rule) or 'manual' (set in the UI)
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
CREATE TABLE IF NOT EXISTS undo (
    run             TEXT NOT NULL,               -- runs.started of the live run that sorted the mail
    message_key     TEXT NOT NULL,
    origin          TEXT,                        -- folder the mail was in before (config notation), NULL = source
    moved_to        TEXT,                        -- where the run put it, as written to processed
    before          TEXT,                        -- JSON of the processed row before the run, NULL = none
    PRIMARY KEY (run, message_key)
);
CREATE TABLE IF NOT EXISTS senders (
    address         TEXT PRIMARY KEY,            -- sender address, lower case
    unsubscribe     TEXT,                        -- JSON list of its List-Unsubscribe links (https, http, mailto)
    one_click       INTEGER NOT NULL DEFAULT 0,  -- 1 = one-click unsubscribe (RFC 8058) offered
    seen            TEXT,                        -- date of the mail the links come from (the newest one)
    unsubscribed    TEXT,                        -- when you unsubscribed, NULL = not
    method          TEXT                         -- how: 'one-click' (sent by Sortroom) or 'manual'
);
CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,            -- last_reconcile: when the log was last reconciled for real
    value           TEXT NOT NULL
);
"""
RUN_RETENTION_DAYS = 180

MOVED, GONE = 1, 2


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)
        # mails decided by the model were logged as 'jev' up to 0.7.10
        self.db.execute("UPDATE processed SET source = 'classifier' WHERE source = 'jev'")
        self.db.commit()
        self._undo: tuple[str, str | None] | None = None

    def track(self, run_started: datetime, origin: str | None = None) -> None:
        """Remember for undo what record() changes from now on: which run, and the folder the mails
        come from (config notation; None = the source folder)."""
        self._undo = (run_started.isoformat(timespec="seconds"), origin)

    def is_processed(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE message_key = ?", (key,)).fetchone() is not None

    def record(self, outcome) -> None:
        d = outcome.decision
        if self._undo:
            before = self.get(outcome.key)
            self.db.execute(
                "INSERT OR REPLACE INTO undo (run, message_key, origin, moved_to, before) VALUES (?,?,?,?,?)",
                (self._undo[0], outcome.key, self._undo[1], outcome.folder,
                 json.dumps(before, ensure_ascii=False) if before else None))
        if getattr(outcome, "unsubscribe", None):
            self.note_unsubscribe([(outcome.sender, *outcome.unsubscribe, outcome.received)], commit=False)
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
                getattr(outcome, "source", "classifier"),
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
        self.db.execute("DELETE FROM undo WHERE run < ?", (cutoff,))
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

    def set_manual_expiry(self, key: str, expires: date | None) -> bool:
        """Expiry date set by hand; a mail marked 'not found' when it was due gets another try."""
        cur = self.db.execute(
            "UPDATE processed SET expires = ?, expiry_checked = 1, "
            "expired_tagged = CASE WHEN expired_tagged = 2 THEN 0 ELSE expired_tagged END WHERE message_key = ?",
            (expires.isoformat() if expires else None, key))
        self.db.commit()
        return cur.rowcount == 1

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
        args = (new, len(old) + 1, old, _like_prefix(old))
        self.db.execute("UPDATE processed SET moved_to = ? || substr(moved_to, ?) "
                        "WHERE moved_to = ? OR moved_to LIKE ? ESCAPE '!'", args)
        self.db.execute("UPDATE undo SET moved_to = ? || substr(moved_to, ?) "
                        "WHERE moved_to = ? OR moved_to LIKE ? ESCAPE '!'", args)
        self.db.execute("UPDATE undo SET origin = ? || substr(origin, ?) "
                        "WHERE origin = ? OR origin LIKE ? ESCAPE '!'", args)
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

    def undo_entries(self, run: str) -> list[dict]:
        """What the run changed in the log, with the mail's row as it is now (None if gone from the log)
        and whether a later run changed the mail again."""
        cur = self.db.execute(
            "SELECT u.message_key, u.origin, u.moved_to, u.before, "
            "EXISTS (SELECT 1 FROM undo l WHERE l.message_key = u.message_key AND l.run > u.run) "
            "FROM undo u WHERE u.run = ?", (run,))
        out = []
        for key, origin, moved_to, before, later in cur.fetchall():
            out.append({"key": key, "origin": origin, "moved_to": moved_to,
                        "before": json.loads(before) if before else None, "later": bool(later),
                        "row": self.get(key)})
        return out

    def undoable_runs(self) -> list[str]:
        """Start times of the runs that can be undone, newest first."""
        return [r for (r,) in self.db.execute("SELECT DISTINCT run FROM undo ORDER BY run DESC")]

    def undo(self, run: str, restore: list[dict], forget: list[str], back: dict[str, str | None],
             keep: Iterable[str] = ()) -> None:
        """Apply an undo to the log: `restore` rows as they were, `forget` keys (sorted again at the
        next run), `back` gives other keys their old folder and takes their star. Afterwards only the
        mails in `keep` (that could not be changed) can still be undone."""
        for row in restore:
            names = list(row)
            self.db.execute(f"INSERT OR REPLACE INTO processed ({', '.join(names)}) "
                            f"VALUES ({', '.join('?' * len(names))})", [row[n] for n in names])
        self.db.executemany("DELETE FROM processed WHERE message_key = ?", [(k,) for k in forget])
        self.db.executemany("UPDATE processed SET moved_to = ?, flagged = 0 WHERE message_key = ?",
                            [(folder, k) for k, folder in back.items()])
        keep = set(keep)
        self.db.executemany("DELETE FROM undo WHERE run = ? AND message_key = ?",
                            [(run, k) for (k,) in self.db.execute("SELECT message_key FROM undo WHERE run = ?",
                                                                    (run,)).fetchall() if k not in keep])
        self.db.commit()

    def note_unsubscribe(self, found: Iterable[tuple[str, list[str], bool, str | None]], commit: bool = True) -> None:
        """Remember the unsubscribe links of senders: (address, links, one-click?, date of the mail).
        The links of a sender's newest mail win."""
        self.db.executemany(
            "INSERT INTO senders (address, unsubscribe, one_click, seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (address) DO UPDATE SET unsubscribe = excluded.unsubscribe, "
            "one_click = excluded.one_click, seen = excluded.seen "
            "WHERE COALESCE(julianday(excluded.seen), 0) >= COALESCE(julianday(senders.seen), 0)",
            [((address or "").lower(), json.dumps(links), int(one_click), seen)
             for address, links, one_click, seen in found if address])
        if commit:
            self.db.commit()

    def sender(self, address: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM senders WHERE address = ?", (address.lower(),))
        row = cur.fetchone()
        if not row:
            return None
        out = dict(zip([c[0] for c in cur.description], row))
        out["unsubscribe"] = json.loads(out["unsubscribe"] or "[]")
        return out

    def set_unsubscribed(self, address: str, method: str | None) -> bool:
        """Note that you unsubscribed from a sender now (method 'one-click' or 'manual'), or forget it (None)."""
        cur = self.db.execute(
            "UPDATE senders SET unsubscribed = ?, method = ? WHERE address = ?",
            (datetime.now().isoformat(timespec="seconds") if method else None, method, address.lower()))
        self.db.commit()
        return cur.rowcount == 1

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def _like_prefix(folder: str) -> str:
    """LIKE pattern for subfolders of `folder`, with LIKE wildcards in the name escaped by '!'."""
    escaped = folder.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return escaped + "/%"


def last_reconcile(workspace: Path) -> datetime | None:
    """When the mailbox's log was last reconciled for real; None if never. Opens the log read-only."""
    path = workspace / "data" / "state.db"
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'last_reconcile'").fetchone()
    except sqlite3.OperationalError:  # a log from before the meta table
        return None
    finally:
        db.close()
    return datetime.fromisoformat(row[0]) if row else None
