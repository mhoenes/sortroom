"""Work out when a time-limited offer expires.

The model only answers with choices and probabilities, never dates, so the date comes
from two sources: an explicit deadline in the text ("gültig bis 30.09.") wins;
otherwise the model's rough window ("ends within 1-2 days") counted from the send date.

The result is the last day the offer is valid; the mail gets tagged the day after.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# choice criteria for the model's rough window, and how many days after sending each ends
WINDOWS = {
    "same_day": "Ends on the day the email was sent: today, tonight, until midnight, only today, last hours",
    "one_two_days": "Ends within 1-2 days: tomorrow, 24 or 48 hours, last day, last chance",
    "within_week": "Ends within a week: this weekend, a few days, 72 hours, this week",
    "within_month": "Ends within a month: this month, in a few weeks, end of the month",
    "later_or_unknown": "Ends more than a month later, or no end date is stated",
}
WINDOW_DAYS = {"same_day": 0, "one_two_days": 2, "within_week": 7, "within_month": 30}

MAX_DAYS_AHEAD = 180  # anything further away is more likely a different date than a deadline

MONTHS = {
    "januar": 1, "jan": 1, "january": 1,
    "februar": 2, "feb": 2, "february": 2,
    "märz": 3, "maerz": 3, "mär": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))

# words that introduce a deadline; the date must follow within a few words
_CUE = re.compile(
    r"\b(?:gültig\s+bis|bis|endet|läuft|ablaufdatum|ablauf|until|ends?|expires?|valid\s+(?:until|through|thru))\b",
    re.IGNORECASE,
)
_CUE_REACH = 25  # max characters between cue and date ("bis einschließlich Sonntag, 30.09.")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.(\d{4}|\d{2})?(?!\d)")
_DAY_MONTH = re.compile(rf"(?<!\d)(\d{{1,2}})(?:\.|st|nd|rd|th)?\s+({_MONTH})\b\.?(?:\s+(\d{{4}}))?", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE)


def _build(day: int, month: int, year: str | None, sent: date) -> date | None:
    if year:
        y = int(year) + (2000 if len(year) == 2 else 0)
    else:
        y = sent.year
    try:
        d = date(y, month, day)
        if not year and d < sent - timedelta(days=1):  # "bis 05.01." sent in December
            d = date(y + 1, month, day)
        if not sent - timedelta(days=1) <= d <= sent + timedelta(days=MAX_DAYS_AHEAD):
            return None
    except (ValueError, OverflowError):  # no such day, or beyond the last date Python knows
        return None
    return d


def _date_after_cue(window: str, sent: date) -> date | None:
    m = _NUMERIC.search(window)
    if m and m.start() <= _CUE_REACH:
        return _build(int(m[1]), int(m[2]), m[3], sent)
    m = _DAY_MONTH.search(window)
    if m and m.start() <= _CUE_REACH:
        return _build(int(m[1]), MONTHS[m[2].lower()], m[3], sent)
    m = _MONTH_DAY.search(window)
    if m and m.start() <= _CUE_REACH:
        return _build(int(m[2]), MONTHS[m[1].lower()], m[3], sent)
    return None


def find_deadline(text: str, sent: date) -> date | None:
    """Earliest explicit deadline like 'gültig bis 30.09.' or 'ends October 3'."""
    found = []
    for cue in _CUE.finditer(text):
        d = _date_after_cue(text[cue.end(): cue.end() + _CUE_REACH + 25], sent)
        if d:
            found.append(d)
    return min(found) if found else None


def window_deadline(window: str | None, sent: date) -> date | None:
    days = WINDOW_DAYS.get(window or "")
    try:
        return None if days is None else sent + timedelta(days=days)
    except OverflowError:
        return None


def resolve_expiry(text: str, sent: date, window: str | None) -> date | None:
    return find_deadline(text, sent) or window_deadline(window, sent)
