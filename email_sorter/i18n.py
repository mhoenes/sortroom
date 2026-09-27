"""UI translations: English source strings, one JSON catalog per further language.

Templates use _('…') and ngettext('…', '…', n) from Jinja's i18n extension (newstyle: placeholders
are %(name)s, filled from keyword arguments). Python code that produces UI text uses the same _()
and ngettext(). The language is one global setting, [ui] language in config.toml; a middleware
sets it for each request and background jobs inherit it.

A catalog maps an English message to its translation, or a singular message to [singular,
plural] for ngettext. A message missing from the catalog shows in English.
"""
from __future__ import annotations

import contextvars
import json
import tomllib
from datetime import datetime
from pathlib import Path

LANGUAGES = {"en": "English", "de": "Deutsch"}
DEFAULT_LANGUAGE = "en"

_LOCALE = Path(__file__).with_name("locale")
CATALOGS: dict[str, dict[str, str | list[str]]] = {
    code: json.loads((_LOCALE / f"{code}.json").read_text(encoding="utf-8")) for code in LANGUAGES if code != "en"}

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("ui_language", default=None)


def language() -> str:
    return _current.get() or DEFAULT_LANGUAGE


def set_language(code: str | None) -> contextvars.Token:
    return _current.set(code if code in LANGUAGES else None)


def reset_language(token: contextvars.Token) -> None:
    _current.reset(token)


_config_cache: dict[Path, tuple[float, str | None]] = {}


def configured_language(config_path: Path) -> str:
    """[ui] language from config.toml, re-read only when the file changes."""
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return DEFAULT_LANGUAGE
    cached = _config_cache.get(config_path)
    if cached is None or cached[0] != mtime:
        try:
            code = tomllib.loads(config_path.read_text(encoding="utf-8")).get("ui", {}).get("language")
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            code = None
        cached = (mtime, code if code in LANGUAGES else None)
        _config_cache[config_path] = cached
    return cached[1] or DEFAULT_LANGUAGE


# ---------------------------------------------------------------- messages

def translate(message: str) -> str:
    """The translation of message, without filling in placeholders (what Jinja's newstyle wants)."""
    entry = CATALOGS.get(language(), {}).get(message)
    if isinstance(entry, list):
        entry = entry[0]
    return entry or message


def translate_plural(singular: str, plural: str, n: int) -> str:
    entry = CATALOGS.get(language(), {}).get(singular)
    if isinstance(entry, list) and len(entry) == 2:
        return entry[0] if n == 1 else entry[1]
    return singular if n == 1 else plural


def gettext(message: str, **variables) -> str:
    text = translate(message)
    return text % variables if variables else text


def ngettext(singular: str, plural: str, n: int, **variables) -> str:
    return translate_plural(singular, plural, n) % {"num": n, **variables}


_ = gettext


# ---------------------------------------------------------------- numbers and dates

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def num(value, decimals: int = 0) -> str:
    """12345 -> '12,345' (en) / '12 345' (de)."""
    if value is None:
        return "–"
    s = f"{value:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",") if language() == "de" else s


def conf(value) -> str:
    """A probability with two decimals: '0.46' (en) / '0,46' (de)."""
    if value is None:
        return "–"
    s = f"{value:.2f}"
    return s.replace(".", ",") if language() == "de" else s


def _date_only(d) -> str:
    return d.strftime("%d.%m.%Y") if language() == "de" else f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def dt(value: str | None, with_year: bool = False) -> str:
    """A timestamp in local time; the year only when asked for, or instead of the time for an earlier year."""
    if not value:
        return "–"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo:
        stamp = stamp.astimezone()  # a mail's Date header carries the sender's offset; show local time
    time = stamp.strftime("%H:%M")
    if with_year:
        return f"{_date_only(stamp)} {time}"
    if stamp.year != datetime.now().year:
        return _date_only(stamp)  # from an earlier year: the date matters, not the time
    day = stamp.strftime("%d.%m.") if language() == "de" else f"{stamp.day} {_MONTHS[stamp.month - 1]}"
    return f"{day} {time}"


def date(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return _date_only(datetime.fromisoformat(value))
    except ValueError:
        return value


def ago(value: str | None) -> str:
    if not value:
        return _("never")
    try:
        delta = datetime.now() - datetime.fromisoformat(value)
    except ValueError:
        return value
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return _("just now")
    if minutes < 60:
        return _("%(n)s min ago", n=minutes)
    if minutes < 48 * 60:
        return _("%(n)s h ago", n=minutes // 60)
    return _("%(n)s days ago", n=minutes // 1440)
