"""Shared test helpers."""
from pathlib import Path
from types import SimpleNamespace

from email_sorter.config import EXAMPLE_MAILBOXES, Config, _read_toml, config_from_raw

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def example_config() -> Config:
    """The built-in example mailbox with the repository's shared [classifier] settings."""
    return config_from_raw({**_read_toml(EXAMPLE_MAILBOXES["de"]), "classifier": _read_toml(CONFIG)["classifier"]}, "example")


def raw_headers(message_id: str, sender: str = "s@example.org", subject: str = "x",
                date: str = "Mon, 21 Sep 2026 10:00:00 +0200", **extra: str) -> bytes:
    """Header lines as an IMAP server sends them; extra: list_unsubscribe="<https://…>" etc."""
    lines = {"Message-ID": message_id, "From": sender, "Subject": subject, "Date": date,
             **{k.replace("_", "-").title(): v for k, v in extra.items()}}
    return "".join(f"{k}: {v}\r\n" for k, v in lines.items()).encode() + b"\r\n"


class HeaderFetch:
    """Mixin for fake mailboxes: answers imap.header_fields (UID SEARCH, then UID FETCH of some header
    lines) from raw_mails(), the raw headers of the selected folder."""

    fetch_commands: list

    def raw_mails(self) -> list[bytes]:
        raise NotImplementedError

    def uids(self, *a, **kw):
        return [str(i) for i in range(1, len(self.raw_mails()) + 1)]

    @property
    def client(self):
        return SimpleNamespace(uid=self._uid_command)

    def _uid_command(self, command, uid_list, parts):
        self.__dict__.setdefault("fetch_commands", []).append(parts)
        mails, data = self.raw_mails(), []
        for uid in uid_list.split(","):
            raw = mails[int(uid) - 1]
            data += [(f"{uid} (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", data
