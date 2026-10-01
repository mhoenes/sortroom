"""IMAP helpers shared by the sorting, the maintenance tasks and the UI: connecting, folders, moving,
finding mails by message key, fetching a few header lines - nothing that decides about a mail."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from collections.abc import Iterable, Iterator

from imap_tools import AND, MailBox, MailMessage

from .config import Config, Credentials
from .mailtext import message_key
from .oauth import sign_in

log = logging.getLogger(__name__)

IMAP_TIMEOUT = 120  # seconds; a stalled connection raises instead of hanging forever
# some servers reject IMAP command lines over ~20 KB (seen with Strato): at most this many UIDs per command
UID_CHUNK = 250
# whole mails (attachments included) per FETCH: the answer is held in memory at once, and a few
# large attachments per batch must not exhaust a small NAS or Raspberry Pi
BODY_CHUNK = 20


@contextmanager
def connect(cfg: Config, creds: Credentials):
    """The mailbox's IMAP connection, logged in (password or OAuth) and in its source folder."""
    with sign_in(MailBox(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT), creds, cfg.source_folder) as mb:
        yield mb


def chunks(items: list[str], size: int | None = None):
    size = size or UID_CHUNK
    for i in range(0, len(items), size):
        yield items[i:i + size]


_INTERNALDATE = re.compile(rb'UID (\d+) INTERNALDATE "([^"]+)"|INTERNALDATE "([^"]+)" UID (\d+)')


def received_times(mb: MailBox, uids: list[str]) -> dict[str, datetime]:
    """When each mail arrived on the server (IMAP INTERNALDATE), not the sender's Date header."""
    times: dict[str, datetime] = {}
    for chunk in chunks(uids):
        typ, data = mb.client.uid("FETCH", ",".join(chunk), "(INTERNALDATE)")
        if typ != "OK":
            raise RuntimeError(f"INTERNALDATE fetch failed: {typ} {data!r}")
        for item in data:
            line = item[0] if isinstance(item, tuple) else item
            m = _INTERNALDATE.search(line or b"")
            if not m:
                continue
            uid, stamp = (m[1], m[2]) if m[1] else (m[4], m[3])
            try:
                times[uid.decode()] = datetime.strptime(stamp.decode().strip(), "%d-%b-%Y %H:%M:%S %z")
            except ValueError:
                log.debug("unparsable INTERNALDATE %r", stamp)
    return times


_FLAGS = re.compile(rb"UID (\d+) FLAGS \(([^)]*)\)|FLAGS \(([^)]*)\) UID (\d+)")


def seen_uids(mb: MailBox, uids: list[str]) -> set[str]:
    r"""UIDs the user has already read (IMAP \Seen). Fetching FLAGS does not change them."""
    seen: set[str] = set()
    for chunk in chunks(uids):
        typ, data = mb.client.uid("FETCH", ",".join(chunk), "(FLAGS)")
        if typ != "OK":
            raise RuntimeError(f"FLAGS fetch failed: {typ} {data!r}")
        for item in data:
            line = item[0] if isinstance(item, tuple) else item
            m = _FLAGS.search(line or b"")
            if not m:
                continue
            uid, flags = (m[1], m[2]) if m[1] else (m[4], m[3])
            if b"\\seen" in flags.lower():
                seen.add(uid.decode())
    return seen


def server_folder(path: str, delim: str) -> str:
    return delim.join(part for part in path.split("/") if part)


def delimiter(mb: MailBox) -> str:
    folders = mb.folder.list()
    for f in folders:
        if f.name.upper() == "INBOX" and f.delim:
            return f.delim
    return next((f.delim for f in folders if f.delim), "/")


def folder_hint(name: str) -> str:
    """Gmail keeps mail in labels, and a label below INBOX is not a place mail can be moved to."""
    if "/" in name and name.split("/", 1)[0].upper() == "INBOX":
        return (f" On Gmail use a top-level label such as {name.split('/', 1)[1]!r} instead of {name!r} "
                "(Categories → Target folder).")
    return ""


def ensure_folder(mb: MailBox, name: str) -> None:
    if not mb.folder.exists(name):
        log.info("creating folder %s", name)
        try:
            mb.folder.create(name)
        except Exception as e:
            raise RuntimeError(f"could not create folder {name}: {e}.{folder_hint(name)}") from None
        mb.folder.subscribe(name, True)


def move_uids(mb: MailBox, uids: list[str], folder: str) -> None:
    """MOVE, creating the folder once if the server says it is missing ([TRYCREATE]) although it
    looked present - Gmail answers a LIST for names it cannot actually hold."""
    try:
        mb.move(uids, folder)
    except Exception as e:
        if "TRYCREATE" not in str(e):
            raise
        log.info("%s is missing on the server, creating it", folder)
        try:
            mb.folder.create(folder)
            mb.folder.subscribe(folder, True)
            mb.move(uids, folder)
        except Exception as e2:
            raise RuntimeError(f"folder {folder} does not exist and could not be used: {e2}."
                               f"{folder_hint(folder)}") from None


def _since(received_values: Iterable[str | None], fallback_days: int) -> date:
    """Earliest received date of a group of mails, for a narrow IMAP SINCE search."""
    dates = []
    for r in received_values:
        try:
            dates.append(date.fromisoformat((r or "")[:10]))
        except ValueError:
            return date.today() - timedelta(days=fallback_days)
    return min(dates) - timedelta(days=1) if dates else date.today()


def find_uids(mb: MailBox, folder: str, wanted: dict[str, str | None], fallback_days: int) -> dict[str, str]:
    """Locate mails by message key in a folder: {key: uid}. `wanted` maps key -> received."""
    mb.folder.set(folder)
    found: dict[str, str] = {}
    since = _since(wanted.values(), fallback_days)
    for head in mb.fetch(AND(date_gte=since), mark_seen=False, headers_only=True, bulk=UID_CHUNK):
        key = message_key(head)
        if key in wanted and head.uid:  # without a UID (an odd server answer) it can't be moved anyway
            found[key] = head.uid
    return found


# the header lines a message key, a sender rule and the senders page need (see mailtext)
HEADER_FIELDS = ("MESSAGE-ID", "FROM", "SUBJECT", "DATE", "LIST-UNSUBSCRIBE", "LIST-UNSUBSCRIBE-POST")


def header_fields(mb: MailBox, fields: Iterable[str] = HEADER_FIELDS) -> Iterator[MailMessage]:
    """Every mail of the selected folder with only these header lines, UID_CHUNK mails per FETCH.

    A reconcile reads every folder, Gmail's "All Mail" included: the whole header of each mail is
    several KB (Received lines, DKIM signatures), a few lines are a few hundred bytes. That keeps a big
    mailbox well within Gmail's daily IMAP download limit."""
    parts = f"(UID BODY.PEEK[HEADER.FIELDS ({' '.join(fields)})])"
    for chunk in chunks(mb.uids()):
        typ, data = mb.client.uid("FETCH", ",".join(chunk), parts)
        if typ != "OK":
            raise RuntimeError(f"header fetch failed: {typ} {data!r}")
        for item in data:
            if isinstance(item, tuple):  # (b'1 (UID 7 BODY[HEADER.FIELDS (…)] {123}', header bytes)
                yield MailMessage([item])


def group_by_folder(rows, cfg: Config, delim: str) -> dict[str, dict[str, str | None]]:
    """rows of (key, moved_to, received) -> {server folder: {key: received}}"""
    groups: dict[str, dict[str, str | None]] = defaultdict(dict)
    for key, moved_to, received in rows:
        groups[server_folder(moved_to or cfg.source_folder, delim)][key] = received
    return groups
