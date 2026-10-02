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
import math
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


_config_cache: dict[Path, tuple[float, dict]] = {}


def ui_settings(config_path: Path) -> dict:
    """The [ui] table of config.toml (language, theme), re-read only when the file changes."""
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return {}
    cached = _config_cache.get(config_path)
    if cached is None or cached[0] != mtime:
        try:
            ui = tomllib.loads(config_path.read_text(encoding="utf-8")).get("ui", {})
        except (OSError, tomllib.TOMLDecodeError):
            ui = {}
        cached = (mtime, ui if isinstance(ui, dict) else {})
        _config_cache[config_path] = cached
    return cached[1]


def configured_language(config_path: Path) -> str:
    """[ui] language from config.toml."""
    code = ui_settings(config_path).get("language")
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


# the appearance of the UI: "auto" follows the system setting of each device, the others override it
THEMES = ("auto", "light", "dark")


def configured_theme(config_path: Path) -> str:
    """[ui] theme from config.toml."""
    theme = ui_settings(config_path).get("theme")
    return theme if theme in THEMES else "auto"


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


def percent(part: int, whole: int) -> str:
    """part of whole in percent: '2.5%' (en) / '2,5 %' (de); one decimal below 10 %."""
    if not whole:
        return "–"
    value = 100 * part / whole
    s = num(value, 1 if 0 < value < 10 else 0)
    return f"{s} %" if language() == "de" else f"{s}%"


USD_MAX_DECIMALS = 6  # what the log keeps per mail


def usd(value, decimals: int = 2) -> str:
    """'$0.00042' (en) / '0,00042 $' (de). At least `decimals` places, more where a small amount would otherwise
    show as $0.00: a model call costs fractions of a cent, so a month's cost needs two significant digits."""
    value = value or 0
    places = decimals
    if 0 < value < 1:
        places = max(decimals, min(USD_MAX_DECIMALS, 1 - math.floor(math.log10(value))))
        if round(value, places) == 0:
            return "<" + usd(10 ** -places, places)
    text = f"{value:,.{places}f}"
    while places > decimals and text.endswith("0"):  # $0.0004, not $0.00040
        text, places = text[:-1], places - 1
    if language() == "de":
        return text.replace(",", " ").replace(".", ",") + "\u00a0$"  # no break between amount and sign
    return f"${text}"


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
