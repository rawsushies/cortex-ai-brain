"""Find a due date in everyday words, in English and Malay. No AI: a small, predictable parser.

    find_date("pay rent by Friday", today)     -> (date, "by Friday")
    find_date("hantar laporan esok", today)    -> (date, "esok")
    find_date("renew passport 12 Dec", today)  -> (date, "12 Dec")

Dates in the past roll forward to the next occurrence (a weekday or "12 Dec" means the next one).
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "isnin": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "selasa": 1,
    "wednesday": 2, "rabu": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "khamis": 3,
    "friday": 4, "fri": 4, "jumaat": 4, "jumat": 4,
    "saturday": 5, "sabtu": 5,  # not "sat"/"sun"/"wed": everyday words
    "sunday": 6, "ahad": 6,
}
MONTHS = {
    "january": 1, "jan": 1, "januari": 1,
    "february": 2, "feb": 2, "februari": 2,
    "march": 3, "mar": 3, "mac": 3,
    "april": 4, "apr": 4,
    "may": 5, "mei": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7, "julai": 7,
    "august": 8, "aug": 8, "ogos": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "okt": 10, "oktober": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12, "dis": 12, "disember": 12,
}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MO = "|".join(sorted(MONTHS, key=len, reverse=True))
_LEAD = r"(?:(?:by|before|on|due|until|this|pada|sebelum|hari)\s+)?"
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "a": 1, "an": 1,
        "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7, "sebulan": 1, "seminggu": 1}

PATTERNS = [  # (regex, kind); first match in the text wins, longer phrases are listed first
    (rf"\b{_LEAD}(\d{{4}})-(\d{{2}})-(\d{{2}})\b", "iso"),
    (rf"\b{_LEAD}(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{2,4}}))?\b", "dmy"),
    (rf"\b{_LEAD}(\d{{1,2}})\.(\d{{1,2}})\.(\d{{2,4}})\b", "dmy"),  # 5.1.2027; never "3.2" (a version number)
    (rf"\b{_LEAD}(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MO})\b\.?(?:\s+(\d{{4}}))?", "d_mon"),
    (rf"\b{_LEAD}({_MO})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", "mon_d"),
    (r"\b(?:the\s+)?day\s+after\s+tomorrow\b|\blusa\b", "+2"),
    (r"\b(?:by\s+)?(?:tomorrow|tmrw|tmr)\b|\b(?:esok|besok)\b", "+1"),
    (r"\b(?:by\s+)?(?:today|tonight|this\s+evening|end\s+of\s+(?:the\s+)?day|eod)\b|\b(?:hari\s+ini|malam\s+ini|petang\s+ini)\b", "+0"),
    (rf"\b(?:next|depan)\s+({_WD})\b|\b({_WD})\s+(?:depan|next\s+week)\b", "next_wd"),
    (rf"\b{_LEAD}({_WD})\b", "wd"),
    (r"\b(?:in|within|dalam)\s+(\d+|one|two|three|four|five|six|seven|a|an|satu|dua|tiga|empat|lima|enam|tujuh)\s+"
     r"(days?|weeks?|months?|hari|minggu|bulan)\b|\b(\d+|dua|tiga|empat|lima)\s+(hari|minggu|bulan)\s+lagi\b", "in_n"),
    (r"\b(?:end\s+of\s+(?:the\s+)?week|this\s+weekend|hujung\s+minggu)\b", "eow"),
    (r"\b(?:next\s+week|minggu\s+depan)\b", "next_week"),
    (r"\b(?:end\s+of\s+(?:the\s+)?month|hujung\s+bulan|akhir\s+bulan)\b", "eom"),
    (r"\b(?:next\s+month|bulan\s+depan)\b", "next_month"),
]
COMPILED = [(re.compile(p, re.I), k) for p, k in PATTERNS]


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _roll(d: date | None, today: date, explicit_year: bool) -> date | None:
    """A day-month without a year that already passed means next year."""
    if d and not explicit_year and d < today:
        return _safe_date(d.year + 1, d.month, d.day)
    return d


def _resolve(m: re.Match, kind: str, today: date) -> date | None:
    g = m.groups()
    if kind == "iso":
        return _safe_date(int(g[0]), int(g[1]), int(g[2]))
    if kind == "dmy":  # day/month like the rest of the world (and Malaysia); not month/day
        y = int(g[2]) if g[2] else today.year
        if g[2] and y < 100:
            y += 2000
        return _roll(_safe_date(y, int(g[1]), int(g[0])), today, bool(g[2]))
    if kind == "d_mon":
        y = int(g[2]) if g[2] else today.year
        return _roll(_safe_date(y, MONTHS[g[1].lower()], int(g[0])), today, bool(g[2]))
    if kind == "mon_d":
        y = int(g[2]) if g[2] else today.year
        return _roll(_safe_date(y, MONTHS[g[0].lower()], int(g[1])), today, bool(g[2]))
    if kind in ("+0", "+1", "+2"):
        return today + timedelta(days=int(kind[1]))
    if kind in ("wd", "next_wd"):
        name = next(x for x in g if x)
        target = WEEKDAYS[name.lower()]
        ahead = (target - today.weekday()) % 7
        if kind == "wd":
            return today + timedelta(days=ahead or 7)  # "Friday" said on a Friday means next Friday
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=target)  # "next Friday" / "jumaat depan": that day next week
    if kind == "in_n":
        num = g[0] or g[2]
        unit = (g[1] or g[3]).lower()
        n = int(num) if num.isdigit() else _NUM.get(num.lower(), 1)
        if unit.startswith(("day", "hari")):
            return today + timedelta(days=n)
        if unit.startswith(("week", "minggu")):
            return today + timedelta(weeks=n)
        return _add_months(today, n)
    if kind == "eow":
        return today + timedelta(days=(4 - today.weekday()) % 7 if today.weekday() <= 4 else (6 - today.weekday()))
    if kind == "next_week":
        return today + timedelta(days=7 - today.weekday())  # Monday of next week
    if kind == "eom":
        return date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if kind == "next_month":
        return _add_months(today.replace(day=1), 1)
    return None


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def find_date(text: str, today: date | None = None) -> tuple[date | None, str]:
    """The first due date mentioned in `text` and the words that said it ("" if none)."""
    today = today or date.today()
    best = None
    for rx, kind in COMPILED:
        for m in rx.finditer(text):
            d = _resolve(m, kind, today)
            if d and (best is None or m.start() < best[2]):
                best = (d, m.group(0).strip(), m.start())
            if d:
                break
    return (best[0], best[1]) if best else (None, "")


def strip_date(text: str, phrase: str) -> str:
    """Remove the date words from a task's text: 'pay rent by Friday' -> 'pay rent'."""
    if not phrase:
        return text.strip()
    out = text.replace(phrase, " ", 1)
    out = re.sub(r"\s+(?:by|before|on|due|pada|sebelum)\s*$", "", out.strip(), flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip(" ,;-")
