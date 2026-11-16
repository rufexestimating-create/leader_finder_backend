"""Companies from business directories. BBB (Better Business Bureau) for now: its search lists the companies of a kind
in a city, and each company's BBB profile gives its website, owner's name and when it started.

A plain request is tried first; when BBB turns it away, FlareSolverr opens the page instead. One page at a time with a
short pause, so the directory isn't hammered.
"""
import json
import logging
import re
import time
from urllib.parse import quote_plus, urljoin

import httpx

from app.services.lead_finder import flaresolverr, website

log = logging.getLogger("uvicorn.error")

BBB = "https://www.bbb.org"
PER_PAGE = 15
MAX_PAGES = 70
PAUSE = 1.0
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en"}

# Company type -> what to type into BBB's search
_BBB_TEXT = {"gc": "general contractor", "sub": "contractor", "design_build": "design build", "cm": "construction management",
             "developer": "real estate developer", "home_builder": "home builder", "remodeler": "remodeling",
             "architect": "architect", "engineer": "engineering", "interior": "interior designer",
             "supplier": "building supplies"}
_STATE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)
_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_VISIT = re.compile(r'<a href="(https?://[^"]+)"[^>]*>(?:(?!</a>).){0,3000}?Visit Website</a>', re.S)


class DirectoryError(Exception):
    pass


def _open(client: httpx.Client, url: str) -> str | None:
    try:
        res = client.get(url)
        if res.status_code == 200 and len(res.text) > 2000:
            return res.text
    except httpx.HTTPError:
        pass
    got = flaresolverr.get(url)
    return got[1] if got else None


def _profile(client: httpx.Client, url: str) -> dict:
    """{website, people, founded_year} from a BBB profile page"""
    html = _open(client, url) or ""
    out = {"website": "", "people": [], "founded_year": None}
    m = _VISIT.search(html)
    if m:
        out["website"] = m.group(1)
    for raw in _LD.findall(html):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "LocalBusiness":
                continue
            year = str(item.get("foundingDate") or "")[:4]
            out["founded_year"] = int(year) if year.isdigit() else None
            for e in item.get("employee") or []:
                name = " ".join(x for x in (e.get("givenName"), e.get("familyName")) if x) or e.get("name") or ""
                if website.looks_like_person(name):
                    out["people"].append({"name": name, "title": e.get("jobTitle") or "", "source": url})
    return out


def bbb(company_type: str | None, city: str, state: str | None, limit: int, skip=lambda name, phone: False) -> list[dict]:
    """[{name, website, phone, address, rating, people, founded_year, bbb_url}] for companies with a website.
    `skip(name, phone)` leaves out companies already found elsewhere before their profile is opened."""
    text = _BBB_TEXT.get(company_type or "")
    if not text or not city:
        return []
    place = f"{city}, {state}" if state else city
    out: list[dict] = []
    with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(25.0, connect=8.0), follow_redirects=True) as client:
        for page in range(1, min(MAX_PAGES, -(-limit // PER_PAGE) + 2) + 1):
            url = f"{BBB}/search?find_country=USA&find_text={quote_plus(text)}&find_loc={quote_plus(place)}&page={page}"
            html = _open(client, url)
            if html is None:
                if page == 1:
                    raise DirectoryError("BBB couldn't be searched right now")
                break
            m = _STATE.search(html)
            try:
                results = json.loads(m.group(1))["searchResult"]["results"] if m else []
            except (ValueError, KeyError, TypeError):
                results = []
            for r in results:
                name, phones = (r.get("businessName") or "").strip(), r.get("phone") or []
                phone = phones[0] if phones else None
                if not name or not r.get("reportUrl") or r.get("outOfBusinessStatus") or skip(name, phone):
                    continue
                time.sleep(PAUSE)
                profile_url = urljoin(BBB, r["reportUrl"])
                prof = _profile(client, profile_url)
                if not prof["website"]:
                    continue
                address = ", ".join(x for x in (r.get("address"), r.get("city"), r.get("state")) if x)
                out.append({"name": name, "website": prof["website"], "phone": phone, "address": address or None,
                            "rating": r.get("rating"), "people": prof["people"], "founded_year": prof["founded_year"],
                            "bbb_url": profile_url})
                if len(out) >= limit:
                    return out
            if len(results) < PER_PAGE:
                break
            time.sleep(PAUSE)
    return out
