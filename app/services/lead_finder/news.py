"""Recent news about a company, as facts for the AI writer. A headline is kept only when the company's name is in it, it is
recent, and it isn't bad news (a lawsuit, a lien, an accident): opening an email with the company's worst week would be
worse than saying nothing."""
import re
from datetime import datetime, timedelta

from app.services.lead_finder import website

MAX_NEWS = 3
MAX_DAYS = 183

_BAD = re.compile(
    r"\b(lawsuit|sued|sues|suing|lien|fraud|bankrupt\w*|insolven\w*|fatal\w*|death|dies|died|killed|injur\w*|accident|arrest\w*|"
    r"scandal|fined|fine|penalt\w*|violation|collapse\w*|fire|layoffs?|laid off|investigation|probe|indict\w*|recall|"
    r"settle(?:s|d|ment)?|default\w*|strike|breach\w*|hack\w*|cyber\w*|ransomware|leak\w*|stolen|theft|outage|shutdown|shuts down)\b", re.I)


def _recent(when: str | None, today: datetime) -> bool:
    """Serper gives "5 days ago", "3 weeks ago", "7 months ago" or a date like "Nov 6, 2026"; unknown = not recent"""
    w = (when or "").strip().lower()
    m = re.match(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", w)
    if m:
        days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)] * int(m.group(1))
        return days <= MAX_DAYS
    if w in ("yesterday", "today"):
        return True
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return today - datetime.strptime(when.strip(), fmt) <= timedelta(days=MAX_DAYS)
        except ValueError:
            continue
    return False


def pick(items: list[dict], company_name: str, today: datetime | None = None) -> list[dict]:
    """Up to 3 recent, good-news headlines that name the company: [{text, url, when}]"""
    today = today or datetime.now()
    tokens = website.name_tokens(company_name)
    out: list[dict] = []
    for item in items:
        title = " ".join((item.get("title") or "").split())
        title = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title).strip()  # "Headline - Source": drop the source
        if not title or not item.get("link"):
            continue
        low = title.lower()
        named = bool(tokens) and sum(1 for t in tokens if t in low) >= min(2, len(tokens))
        if not named or _BAD.search(title) or _BAD.search(item.get("snippet") or ""):
            continue
        if not _recent(item.get("date"), today) or any(o["text"].lower() == low for o in out):
            continue
        out.append({"text": title[:130], "url": item["link"], "when": (item.get("date") or "").strip() or None})
        if len(out) >= MAX_NEWS:
            break
    return out
