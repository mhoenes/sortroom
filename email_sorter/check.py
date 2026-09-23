"""--check: verify IMAP and Jev API access without touching any mail."""
from __future__ import annotations

from imap_tools import MailBox

from .config import Config, Credentials
from .jev import JevClient, JevError
from .sorter import _delimiter, server_folder

SAMPLE_STATE = {
    "from": "Stadtwerke Musterstadt <rechnung@stadtwerke-musterstadt.de>",
    "to": "you@example.de",
    "subject": "Ihre Jahresabrechnung Strom 2026",
    "is_mailing_list": False,
    "attachments": ["Jahresabrechnung_2026.pdf"],
    "body": "Sehr geehrte Kundin, sehr geehrter Kunde, anbei erhalten Sie Ihre Jahresabrechnung. "
            "Der Nachzahlungsbetrag von 84,20 EUR wird am 15.10. per Lastschrift eingezogen.",
}


def check(cfg: Config, creds: Credentials) -> int:
    ok = True

    print(f"IMAP  {cfg.imap_host}:{cfg.imap_port} as {creds.imap_user}")
    try:
        with MailBox(cfg.imap_host, cfg.imap_port).login(creds.imap_user, creds.imap_password,
                                                         initial_folder=cfg.source_folder) as mb:
            delim = _delimiter(mb)
            existing = {f.name for f in mb.folder.list()}
            status = mb.folder.status(cfg.source_folder)
            print(f"  OK - {cfg.source_folder} has {status.get('MESSAGES')} mails, "
                  f"folder separator is {delim!r}")
            print("  target folders:")
            for cat in cfg.categories.values():
                if cat.folder:
                    name = server_folder(cat.folder, delim)
                    state = "exists" if name in existing else "will be created on first live run"
                    print(f"    {cat.key:<20} -> {name}  ({state})")
    except Exception as e:
        ok = False
        print(f"  FAILED: {e}")

    print(f"\nJev   {cfg.jev_model} via {cfg.jev_endpoint}")
    try:
        jev = JevClient(creds.jev_api_key, cfg.jev_endpoint, cfg.jev_model, cfg.timeout_seconds)
        d = jev.decide(SAMPLE_STATE, cfg.descriptions)
        print(f"  OK - sample electricity bill -> {d.category} (confidence {d.confidence:.2f}, "
              f"needs_action {d.needs_action:.2f}, cost ${d.cost:.6f})")
    except JevError as e:
        ok = False
        print(f"  FAILED: {e}")

    return 0 if ok else 1
