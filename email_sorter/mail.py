"""Sending mail: the notifications on failed scheduled runs (notify.py) and the daily summary (digest.py).

One SMTP account for all mailboxes, set under Global settings: [mail] in config.toml, its password in
config/secrets.toml ([smtp] password) like the API key. Plain text in the UI language; the daily summary
also as HTML.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from .config import SECRETS_FILE, ConfigError, _read_toml, read_secrets

log = logging.getLogger(__name__)

SECURITY = ("starttls", "ssl", "none")
TIMEOUT = 30


class MailError(Exception):
    pass


@dataclass(frozen=True)
class MailSettings:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    user: str = ""
    sender: str = ""
    recipient: str = ""
    notify_failures: bool = False
    digest: bool = False
    digest_time: str = "07:00"
    ui_url: str = ""          # address of the admin UI, for links in the mails; empty: no links

    @property
    def ready(self) -> bool:
        return bool(self.host and self.sender and self.recipient)

    def link(self, path: str) -> str:
        return self.ui_url.rstrip("/") + path if self.ui_url else ""


def mail_settings(config_path: Path) -> MailSettings:
    """[mail] from config.toml; defaults (nothing sent) when it is missing or unreadable."""
    try:
        raw = _read_toml(config_path).get("mail") or {}
    except (ConfigError, OSError):
        return MailSettings()
    try:
        port = int(raw.get("port", 587))
    except (TypeError, ValueError):
        port = 587
    security = str(raw.get("security", "starttls"))
    return MailSettings(
        host=str(raw.get("host", "")).strip(), port=port, security=security if security in SECURITY else "starttls",
        user=str(raw.get("user", "")).strip(), sender=str(raw.get("sender", "")).strip(),
        recipient=str(raw.get("recipient", "")).strip(), notify_failures=bool(raw.get("notify_failures", False)),
        digest=bool(raw.get("digest", False)), digest_time=str(raw.get("digest_time", "07:00")).strip(),
        ui_url=str(raw.get("ui_url", "")).strip())


def smtp_password(config_path: Path) -> str:
    try:
        return str(read_secrets(config_path.with_name(SECRETS_FILE)).get("smtp", {}).get("password", ""))
    except ConfigError:
        return ""


def send(settings: MailSettings, password: str, subject: str, body: str, html: str = "") -> None:
    """Send one mail to the recipient: plain text, and with `html` also that as the part mail programs show.
    Raises MailError with the server's reason."""
    if not settings.ready:
        raise MailError("sending mail is not set up (server, sender and recipient)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender
    msg["To"] = settings.recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(settings.sender)[1].rpartition("@")[2] or None)
    msg["Auto-Submitted"] = "auto-generated"  # no out-of-office replies to it
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    try:
        if settings.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(settings.host, settings.port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(settings.host, settings.port, timeout=TIMEOUT)
        with server:
            if settings.security == "starttls":
                server.starttls(context=context)
            if settings.user:
                server.login(settings.user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"{settings.host}:{settings.port}: {e}") from None
    log.info("mail sent to %s: %s", settings.recipient, subject)


def send_configured(config_path: Path, subject: str, body: str, html: str = "") -> bool:
    """Send with the configured account; logs instead of raising (for the schedule)."""
    try:
        send(mail_settings(config_path), smtp_password(config_path), subject, body, html)
        return True
    except MailError as e:
        log.error("could not send %r: %s", subject, e)
        return False


def stamp(when: datetime | None = None) -> str:
    return (when or datetime.now()).isoformat(timespec="seconds")
