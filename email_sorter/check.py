"""Connection checks without touching any mail: the IMAP login of a mailbox and the model endpoint.

They are independent: the admin UI checks the mailbox under its settings and the model under
Global settings; --check does both for every mailbox.
"""
from __future__ import annotations

from typing import Callable

from imap_tools import MailBox

from .config import Config, Credentials
from .classifier import ClassifierClient, ClassifierError, Decision
from .oauth import sign_in
from .sorter import IMAP_TIMEOUT, _delimiter, server_folder

SAMPLE_STATE = {
    "from": "Stadtwerke Musterstadt <rechnung@stadtwerke-musterstadt.de>",
    "to": "you@example.de",
    "subject": "Ihre Jahresabrechnung Strom 2026",
    "is_mailing_list": False,
    "attachments": ["Jahresabrechnung_2026.pdf"],
    "body": "Sehr geehrte Kundin, sehr geehrter Kunde, anbei erhalten Sie Ihre Jahresabrechnung. "
            "Der Nachzahlungsbetrag von 84,20 EUR wird am 15.10. per Lastschrift eingezogen.",
}


def check_imap(cfg: Config, creds: Credentials, out: Callable[[str], None] = print) -> bool:
    """Log in, look at the inbox and list which target folders exist."""
    how = f" with {cfg.imap_auth.capitalize()} (OAuth)" if creds.oauth else ""
    out(f"IMAP  {cfg.imap_host}:{cfg.imap_port} as {creds.imap_user}{how}")
    try:
        with sign_in(MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT), creds, cfg.source_folder) as mb:
            delim = _delimiter(mb)
            existing = {f.name for f in mb.folder.list()}
            status = mb.folder.status(cfg.source_folder)
            out(f"  OK - {cfg.source_folder} has {status.get('MESSAGES')} mails, folder separator is {delim!r}")
            out("  target folders:")
            for cat in cfg.categories.values():
                if cat.folder:
                    name = server_folder(cat.folder, delim)
                    state = "exists" if name in existing else "will be created on first live run"
                    out(f"    {cat.key:<20} -> {name}  ({state})")
    except Exception as e:
        out(f"  FAILED: {e}")
        return False
    return True


def check_model(client: ClassifierClient, descriptions: dict[str, str]) -> Decision:
    """Classify one sample mail; raises ClassifierError when the endpoint, model or key don't work."""
    return client.decide(SAMPLE_STATE, descriptions)


def check(cfg: Config, creds: Credentials, out: Callable[[str], None] = print) -> int:
    """--check: the mailbox and the model."""
    ok = check_imap(cfg, creds, out)
    out(f"\nModel {cfg.classifier_model} via {cfg.classifier_endpoint}")
    try:
        d = check_model(cfg.classifier_client(creds.classifier_api_key), cfg.descriptions)
        out(f"  OK - sample electricity bill -> {d.category} (confidence {d.confidence:.2f}, "
            f"needs_action {d.needs_action:.2f}, cost ${d.cost:.6f})")
    except ClassifierError as e:
        ok = False
        out(f"  FAILED: {e}")
    return 0 if ok else 1
