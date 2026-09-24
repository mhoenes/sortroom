"""Turn a single-file setup into the first of several mailboxes.

    python -m email_sorter --migrate-mailbox privat --name Privat [--live]

Creates mailboxes/<id>/mailbox.toml from the [imap], [rules] and [categories.*] sections of
config.toml (comments included) and moves data/state.db and the dry-run reports into
mailboxes/<id>/. config.toml itself is left alone: once a mailbox folder exists, only its [jev]
section is read. Without --live it only shows what it would do.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .config import ConfigError, _MAILBOX_ID_RE, _read_toml
from .sorter import RunResult

log = logging.getLogger(__name__)

MAILBOX_SECTIONS = ("imap", "rules", "categories")
_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")


def split_sections(text: str) -> tuple[str, str]:
    """(shared part, mailbox part) of a config.toml, keeping each section's comments with it.

    Comment and blank lines directly above a section header belong to that section.
    """
    shared: list[str] = []
    mailbox: list[str] = []
    target = shared
    pending: list[str] = []  # comments/blank lines waiting for the next header
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m:
            top = m.group(1).split(".")[0]
            target = mailbox if top in MAILBOX_SECTIONS else shared
            target.extend(pending)
            pending = []
            target.append(line)
        elif not line.strip() or line.lstrip().startswith("#"):
            pending.append(line)
        else:
            target.extend(pending)
            pending = []
            target.append(line)
    (target if target is mailbox else shared).extend(pending)
    return "".join(shared), "".join(mailbox)


def migrate_mailbox(base_dir: Path, config_path: Path, box_id: str, name: str | None, live: bool) -> RunResult:
    if not _MAILBOX_ID_RE.match(box_id):
        log.error("mailbox id %r: use lowercase letters, digits, _ or - (max. 40)", box_id)
        return RunResult(exit_code=2, error="invalid mailbox id")
    root = base_dir / "mailboxes"
    if root.is_dir() and any(root.glob("*/mailbox.toml")):
        log.error("%s already holds mailboxes - nothing to migrate", root)
        return RunResult(exit_code=2, error="already migrated")
    try:
        raw = _read_toml(config_path)
    except ConfigError as e:
        log.error("%s", e)
        return RunResult(exit_code=2, error=str(e))
    if "imap" not in raw:
        log.error("%s has no [imap] section - nothing to migrate", config_path)
        return RunResult(exit_code=2, error="no [imap] in config.toml")

    _, mailbox_part = split_sections(config_path.read_text(encoding="utf-8"))
    title = (name or box_id).replace("\\", "\\\\").replace('"', '\\"')
    mailbox_toml = f'name = "{title}"\n\n{mailbox_part.lstrip()}'
    workspace = root / box_id
    moves = []
    state = base_dir / "data" / "state.db"
    if state.exists():
        moves.append((state, workspace / "data" / "state.db"))
    reports = sorted((base_dir / "reports").glob("*.csv")) if (base_dir / "reports").is_dir() else []
    moves += [(r, workspace / "reports" / r.name) for r in reports]

    verb = "" if live else "dry run: would "
    log.info("%screate %s", verb, workspace / "mailbox.toml")
    for src, dst in moves[:1] + ([(f"{len(reports)} report(s)", workspace / "reports")] if reports else []):
        log.info("%smove %s -> %s", verb, src, dst)
    if not live:
        return RunResult(exit_code=0, live=False, moved=len(moves))

    (workspace / "data").mkdir(parents=True, exist_ok=True)
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    (workspace / "mailbox.toml").write_text(mailbox_toml, encoding="utf-8")
    for src, dst in moves:
        shutil.move(str(src), str(dst))
    log.info("mailbox %r created; the [imap], [rules] and [categories] sections in %s are now ignored "
             "and can be deleted", box_id, config_path.name)
    return RunResult(exit_code=0, live=True, moved=len(moves))
