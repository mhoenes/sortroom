"""Mail when a scheduled run, reconcile or the deletion rules fail - once per incident.

A task counts as failed when it couldn't do its job at all: it stopped with an error (the IMAP login,
the model's key or credit, the classifier unreachable), raised, or the mailbox's login is missing.
Single mails that fail are retried by the next run and don't count. The first failure of a task sends a
mail and is noted in the mailbox's log (meta "alert:<task>"); while it lasts nothing more is sent, and
the first success after it sends "works again". Without [mail] notify_failures nothing is sent or noted.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from . import i18n
from .config import Mailbox
from .i18n import _
from .mail import mail_settings, send_configured, stamp
from .store import Store

log = logging.getLogger(__name__)


def _task_name(task: str) -> str:
    return {"run": _("The scheduled run"), "reconcile": _("The scheduled reconcile"),
            "cleanup": _("The deletion rules")}.get(task, task)


def report(config_path: Path, box: Mailbox, task: str, failed: bool, why: str = "",
           now: datetime | None = None) -> None:
    """Called by the schedule after each task: mail on the first failure, and when it works again."""
    settings = mail_settings(config_path)
    if not (settings.ready and settings.notify_failures):
        return
    key = f"alert:{task}"
    store = Store(box.workspace / "data" / "state.db")
    try:
        since = store.meta(key)
        if failed == bool(since):
            return  # nothing new: still failing, or still fine
        token = i18n.set_language(i18n.configured_language(config_path))
        try:
            link = settings.link(f"/ui/m/{box.id}")
            if failed:
                subject = _("Sortroom: %(task)s of %(mailbox)s failed", task=_task_name(task), mailbox=box.name)
                body = _("%(task)s of the mailbox %(mailbox)s failed at %(when)s:\n\n%(why)s\n\n"
                         "You get another mail when it works again.",
                         task=_task_name(task), mailbox=box.name, when=i18n.dt(stamp(now), True), why=why or "–")
            else:
                subject = _("Sortroom: %(task)s of %(mailbox)s works again", task=_task_name(task), mailbox=box.name)
                body = _("%(task)s of the mailbox %(mailbox)s works again (failing since %(since)s).",
                         task=_task_name(task), mailbox=box.name, since=i18n.dt(since or "", True))
            if link:
                body += "\n\n" + link
        finally:
            i18n.reset_language(token)
        if send_configured(config_path, subject, body + "\n"):
            if failed:
                store.set_meta(key, stamp(now))
            else:
                store.del_meta(key)
    finally:
        store.close()
