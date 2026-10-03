"""The daily summary mail: per mailbox, what waits under To review, the starred mail of the last day,
offers that expire today or tomorrow, and runs with errors. One mail for all mailboxes, sent by the
schedule at [mail] digest_time; none on a day with nothing to report. With [mail] ui_url the mail links
into the admin UI.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

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


@dataclass
class Item:
    title: str           # the subject, or the kind of run
    meta: str            # sender and time
    note: str = ""       # the model's suggestion, the expiry or the error
    tone: str = ""       # of the note: "", "warn" or "err"
    url: str = ""        # the mail in the admin UI


@dataclass
class Group:
    heading: str
    total: int
    items: list[Item]
    url: str = ""        # the list in the admin UI

    @property
    def more(self) -> int:
        return self.total - len(self.items)


def section(box: Mailbox, db: sqlite3.Connection, settings: MailSettings, now: datetime) -> list[Group]:
    """What to report about one mailbox; empty when there is nothing."""
    from .web import queries, run_kind

    cfg = box.cfg

    def label(key: str) -> str:
        return cfg.categories[key].label if key in cfg.categories else key

    def subject(m: dict) -> str:
        return m["subject"] or _("(no subject)")

    def open_mail(list_query: str, m: dict) -> str:  # the list with the mail open beside it
        return settings.link(f"/ui/m/{box.id}/mails?{list_query}&key={quote(m['message_key'], safe='')}")

    day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    groups: list[Group] = []

    review_total = queries.stats(db, cfg.min_confidence, now).uncertain
    if review_total:
        review = queries.uncertain_mails(db, cfg.min_confidence, LIST_MAX, days=30, now=now)
        groups.append(Group(_("To review"), review_total, [
            Item(subject(m), f"{m['sender']} · {i18n.dt(m['received'])}",
                 _("suggestion: %(category)s", category=label(m["category"])),
                 url=open_mail("uncertain=1&period=30d", m)) for m in review],
            settings.link(f"/ui/m/{box.id}/mails?uncertain=1&period=30d")))

    starred = [dict(r) for r in db.execute(
        "SELECT message_key, received, sender, subject FROM processed WHERE flagged = 1 AND gone = 0 "
        "AND processed_at >= ? ORDER BY processed_at DESC", (day_ago,))]
    if starred:
        groups.append(Group(_("Starred in the last 24 hours"), len(starred), [
            Item(subject(m), f"{m['sender']} · {i18n.dt(m['received'])}", url=open_mail("flagged=1&period=7d", m))
            for m in starred[:LIST_MAX]], settings.link(f"/ui/m/{box.id}/mails?flagged=1&period=7d")))

    expiring = [dict(r) for r in db.execute(
        "SELECT message_key, expires, sender, subject FROM processed WHERE expires IN (?, ?) "
        "AND expired_tagged = 0 AND gone = 0 ORDER BY expires, received", (today, tomorrow))]
    if expiring:
        groups.append(Group(_("Offers that expire today or tomorrow"), len(expiring), [
            Item(subject(m), m["sender"], _("expires today") if m["expires"] == today else _("expires tomorrow"),
                 "warn" if m["expires"] == today else "", open_mail("period=30d", m))
            for m in expiring[:LIST_MAX]]))

    failed = [dict(r) for r in db.execute(
        "SELECT started, kind, exit_code, failed, error FROM runs WHERE started >= ? AND live = 1 "
        "AND (exit_code != 0 OR error IS NOT NULL) ORDER BY started DESC", (day_ago,))]
    if failed:
        groups.append(Group(_("Runs with errors in the last 24 hours"), len(failed), [
            Item(run_kind(r["kind"]), i18n.dt(r["started"]),
                 r["error"] or i18n.ngettext("%(num)s error", "%(num)s errors", r["failed"] or 1), "err")
            for r in failed[:LIST_MAX]], settings.link(f"/ui/m/{box.id}")))
    return groups


def text(sections: list[tuple[str, list[Group]]]) -> str:
    """The plain-text part, for mail programs that don't show HTML."""
    out: list[str] = []
    for name, groups in sections:
        out += [name, "=" * len(name)]
        for g in groups:
            out += ["", f"{g.heading}: {g.total}" + (f" – {g.url}" if g.url else "")]
            for item in g.items:
                out += [f"  • {item.title}", "    " + " · ".join(x for x in (item.meta, item.note) if x)]
            if g.more:
                out.append("  " + _("… and %(n)s more", n=g.more))
        out.append("")
    return "\n".join(out) + "\n-- \n" + footer() + "\n"


def footer() -> str:
    return _("The daily summary of Sortroom. Switch it off under Global settings → Mail.")


def compose(config_path: Path, boxes: dict[str, Mailbox],
            now: datetime | None = None) -> tuple[str, str, str] | None:
    """(subject, text, html), or None when no mailbox has anything to report."""
    from .web import queries, templates

    now = now or datetime.now()
    settings = mail_settings(config_path)
    sections: list[tuple[str, list[Group]]] = []
    review = starred = 0
    for box in boxes.values():
        db = queries.connect(box.workspace)
        if db is None:
            continue
        try:
            groups = section(box, db, settings, now)
            review += queries.stats(db, box.cfg.min_confidence, now).uncertain
            starred += db.execute("SELECT COUNT(*) FROM processed WHERE flagged = 1 AND gone = 0 "
                                  "AND processed_at >= ?",
                                  ((now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()[0]
        finally:
            db.close()
        if groups:
            sections.append((box.name, groups))
    if not sections:
        return None
    subject = _("Sortroom: %(review)s to review, %(starred)s starred", review=review, starred=starred)
    html = templates.get_template("mail_digest.html").render(
        subject=subject, day=i18n.date(now.date().isoformat()), sections=sections, footer=footer(),
        home=settings.link("/ui"))
    return subject, text(sections), html


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
    subject, body, html = composed
    return send_configured(config_path, subject, body, html)
