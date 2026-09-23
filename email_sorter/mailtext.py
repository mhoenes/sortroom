"""Turn an imap_tools message into the compact `state` object sent to Jev."""
from __future__ import annotations

import hashlib
import html
import re

from imap_tools import MailMessage

_DROP_BLOCKS = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")
_LINE_BREAKS = re.compile(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>")
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    text = _DROP_BLOCKS.sub(" ", markup)
    text = _LINE_BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    return html.unescape(text)


def clean_body(text: str, max_chars: int) -> str:
    """Drop quoted replies and signatures, squeeze whitespace, truncate."""
    lines = []
    for line in text.splitlines():
        if line.rstrip("\r\n") == "-- ":  # RFC 3676 signature separator
            break
        stripped = _SPACES.sub(" ", line).strip()
        if stripped.startswith(">"):
            continue
        lines.append(stripped)
    body = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    return body[:max_chars]


def build_state(msg: MailMessage, max_chars: int) -> dict:
    body = msg.text if msg.text.strip() else html_to_text(msg.html)
    return {
        "from": msg.from_values.full if msg.from_values else msg.from_,
        "to": ", ".join(msg.to),
        "subject": msg.subject,
        "is_mailing_list": "list-unsubscribe" in msg.headers,
        "attachments": [a.filename for a in msg.attachments if a.filename][:10],
        "body": clean_body(body, max_chars),
    }


def message_key(msg: MailMessage) -> str:
    """Stable id across folder moves: Message-ID, or a hash of basic headers."""
    message_id = (msg.headers.get("message-id") or ("",))[0].strip()
    if message_id:
        return message_id
    raw = f"{msg.from_}|{msg.subject}|{msg.date_str}".encode("utf-8", "replace")
    return "sha1:" + hashlib.sha1(raw).hexdigest()
