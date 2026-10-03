"""The daily summary mail: per mailbox, what waits under To review, the starred mail of the last day,
offers that expire today or tomorrow, and runs with errors. One mail for all mailboxes, sent by the
schedule at [mail] digest_time; none on a day with nothing to report. With [mail] ui_url the mail links
into the admin UI.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, time, timedelta
from pathlib import Path

from . import i18n
from .config import Mailbox
from .i18n import _
from .mail import MailSettings, mail_settings, send_configured

log = logging.getLogger(__name__)

SENT_FILE = ".digest-sent"   # in mailboxes/: the day the summary was last sent
LIST_MAX = 5                 # mails named per list; the rest is counted


def digest_time(settings: MailSettings) -> time:
    try:
        hour, minute = (int(x) for x in settings.digest_time.split(":"))
        return time(hour, minute)
    except ValueError:
        return time(7, 0)


def last_sent(mailboxes_dir: Path) -> str | None:
    try:
        return (mailboxes_dir / SENT_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def due(config_path: Path, mailboxes_dir: Path, now: datetime | None = None) -> bool:
    """Switched on, past today's time, and not sent yet today."""
    now = now or datetime.now()
    settings = mail_settings(config_path)
    if not (settings.ready and settings.digest) or now.time() < digest_time(settings):
        return False
    return last_sent(mailboxes_dir) != now.date().isoformat()


def _names(rows: list[str], total: int) -> list[str]:
    out = [f"  • {r}" for r in rows[:LIST_MAX]]
    if total > len(out):
        out.append("  " + _("… and %(n)s more", n=total - len(out)))
    return out


def section(box: Mailbox, db: sqlite3.Connection, settings: MailSettings, now: datetime) -> list[str]:
    """The lines about one mailbox; empty when there is nothing to report."""
    from .web import queries, run_kind

    cfg = box.cfg

    def label(key: str) -> str:
        return cfg.categories[key].label if key in cfg.categories else key

    day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    lines: list[str] = []

    review_total = queries.stats(db, cfg.min_confidence, now).uncertain
    if review_total:
        review = queries.uncertain_mails(db, cfg.min_confidence, LIST_MAX, days=30, now=now)
        lines.append(_("To review: %(n)s", n=review_total)
                     + _link(settings, f"/ui/m/{box.id}/mails?uncertain=1&period=30d"))
        lines += _names([f"{m['subject'] or _('(no subject)')} · {m['sender']} – "
                         + _("suggestion: %(category)s", category=label(m["category"])) for m in review], review_total)

    starred = [dict(r) for r in db.execute(
        "SELECT received, sender, subject FROM processed WHERE flagged = 1 AND gone = 0 AND processed_at >= ? "
        "ORDER BY processed_at DESC", (day_ago,))]
    if starred:
        lines.append(_("Starred in the last 24 hours: %(n)s", n=len(starred))
                     + _link(settings, f"/ui/m/{box.id}/mails?flagged=1&period=7d"))
        lines += _names([f"{i18n.dt(m['received']) if m['received'] else '–'} · {m['sender']} · "
                         f"{m['subject'] or _('(no subject)')}" for m in starred], len(starred))

    expiring = [dict(r) for r in db.execute(
        "SELECT expires, sender, subject FROM processed WHERE expires IN (?, ?) AND expired_tagged = 0 AND gone = 0 "
        "ORDER BY expires, received", (today, tomorrow))]
    if expiring:
        lines.append(_("Offers that expire today or tomorrow: %(n)s", n=len(expiring)))
        lines += _names([_("valid until %(date)s", date=i18n.date(m["expires"])) + f" · {m['sender']} · "
                         f"{m['subject'] or _('(no subject)')}" for m in expiring], len(expiring))

    failed = [dict(r) for r in db.execute(
        "SELECT started, kind, exit_code, failed, error FROM runs WHERE started >= ? AND live = 1 "
        "AND (exit_code != 0 OR error IS NOT NULL) ORDER BY started DESC", (day_ago,))]
    if failed:
        lines.append(_("Runs with errors in the last 24 hours: %(n)s", n=len(failed))
                     + _link(settings, f"/ui/m/{box.id}"))
        lines += _names([f"{i18n.dt(r['started'])} · {run_kind(r['kind'])}: "
                         + (r["error"] or i18n.ngettext("%(num)s error", "%(num)s errors", r["failed"] or 1))
                         for r in failed], len(failed))
    return lines


def _link(settings: MailSettings, path: str) -> str:
    url = settings.link(path)
    return f" – {url}" if url else ""


def compose(config_path: Path, boxes: dict[str, Mailbox], now: datetime | None = None) -> tuple[str, str] | None:
    """(subject, body), or None when no mailbox has anything to report."""
    from .web import queries

    now = now or datetime.now()
    settings = mail_settings(config_path)
    parts: list[str] = []
    review = starred = 0
    for box in boxes.values():
        db = queries.connect(box.workspace)
        if db is None:
            continue
        try:
            lines = section(box, db, settings, now)
            review += queries.stats(db, box.cfg.min_confidence, now).uncertain
            starred += db.execute("SELECT COUNT(*) FROM processed WHERE flagged = 1 AND gone = 0 "
                                  "AND processed_at >= ?",
                                  ((now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()[0]
        finally:
            db.close()
        if lines:
            parts.append("\n".join([box.name, "=" * len(box.name), *lines]))
    if not parts:
        return None
    subject = _("Sortroom: %(review)s to review, %(starred)s starred", review=review, starred=starred)
    footer = _("The daily summary of Sortroom. Switch it off under Global settings → Mail.")
    return subject, "\n\n".join(parts) + "\n\n-- \n" + footer + "\n"


def send_digest(config_path: Path, load_boxes: Callable[[], dict[str, Mailbox]], mailboxes_dir: Path,
                now: datetime | None = None) -> bool:
    """Compose and send today's summary; noted as sent first, so a failing server isn't tried every tick."""
    now = now or datetime.now()
    try:
        (mailboxes_dir / SENT_FILE).write_text(now.date().isoformat(), encoding="utf-8")
    except OSError as e:
        log.error("daily summary: cannot note the day in %s: %s", mailboxes_dir, e)
        return False
    token = i18n.set_language(i18n.configured_language(config_path))
    try:
        composed = compose(config_path, load_boxes(), now)
    finally:
        i18n.reset_language(token)
    if composed is None:
        log.info("daily summary: nothing to report today")
        return True
    return send_configured(config_path, *composed)
