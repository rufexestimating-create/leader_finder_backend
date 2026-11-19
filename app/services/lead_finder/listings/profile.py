"""One company's page on a directory: its name, own website, phone, emails, address, city and state.

The website always comes from the directory's own "Website" field or button; when there is none it stays empty rather
than guessing from some other outbound link (sponsor banners, Facebook, the directory's own pages).
"""
import html as html_lib
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.services.lead_finder.listings.email_fix import fix_email

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_CODE_BY_NAME = {v.lower(): k for k, v in STATE_NAMES.items()}

SKIP_HOSTS = ("houzz.com", "yelp.com", "yellowpages.com", "google.com", "apple.com", "facebook.com", "instagram.com",
              "twitter.com", "x.com", "linkedin.com", "youtube.com", "pinterest.com", "tiktok.com", "w3.org", "schema.org")
_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_JUNK_EMAIL = ("example.com", "domain.com", "yoursite.com", "wixpress.com", "sentry.io", "w3.org", "schema.org", "wordpress.org",
               "bootstrap", "cloudflare", "gravatar", "placeholder")
_FILE_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".css", ".js")
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_WEBSITE_LABEL = re.compile(r"^(visit\s+)?(our\s+)?web\s?site\b", re.I)
_CITY_STATE = re.compile(r"([A-Z][A-Za-z .'\-]{1,40}),\s*([A-Z]{2})\b")


def _empty(url: str) -> dict:
    return {"name": "", "url": url, "website": "", "emails": [], "phone": "", "address": "", "city": "", "state": ""}


def _cf_email(hex_str: str) -> str:
    """Cloudflare's hidden email (data-cfemail / #hex links)"""
    try:
        k = int(hex_str[:2], 16)
        return "".join(chr(int(hex_str[i:i + 2], 16) ^ k) for i in range(2, len(hex_str), 2)).strip()
    except ValueError:
        return ""


def _clean(emails: list[str], page_url: str) -> list[str]:
    own = urlparse(page_url).netloc.lower().removeprefix("www.")
    out = []
    for raw in emails:
        e = fix_email(raw)
        if not e or any(j in e for j in _JUNK_EMAIL) or e.endswith(_FILE_ENDINGS):
            continue
        if own and e.split("@")[-1].endswith(own):
            continue  # the directory's own address, in every footer
        if e not in out:
            out.append(e)
    return out


def _emails(soup: BeautifulSoup, text: str, page_url: str) -> list[str]:
    found = [a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].strip().lower().startswith("mailto:")]
    found += [_cf_email(t["data-cfemail"]) for t in soup.find_all(attrs={"data-cfemail": True})]
    found += _EMAIL.findall(text)
    return _clean(found, page_url)


def city_state(text: str) -> tuple[str, str]:
    """("Houston", "TX") from "…, Houston, TX 77002"; ("", "") when there is no US city and state in it"""
    for city, code in reversed(_CITY_STATE.findall(text or "")):
        if code in US_STATES:
            return city.split(",")[-1].strip(), code
    return "", ""


def state_name(code_or_name: str) -> str:
    """"TX" or "texas" -> "Texas"; anything else -> "" """
    v = (code_or_name or "").strip()
    if v.upper() in STATE_NAMES:
        return STATE_NAMES[v.upper()]
    return STATE_NAMES.get(_CODE_BY_NAME.get(v.lower(), ""), "")


def _json_ld(soup: BeautifulSoup) -> dict:
    out: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            if not any(k in str(item.get("@type", "")) for k in ("Business", "Organization", "Contractor", "ProfessionalService")):
                continue
            out.setdefault("name", item.get("name"))
            out.setdefault("phone", item.get("telephone"))
            url = item.get("url") or item.get("sameAs")
            if isinstance(url, str) and urlparse(url).netloc and not any(s in urlparse(url).netloc.lower() for s in SKIP_HOSTS):
                out.setdefault("website", url)
            addr = item.get("address")
            if isinstance(addr, dict):
                out.setdefault("address", ", ".join(x for x in (addr.get("streetAddress"), addr.get("addressLocality"),
                                                                  addr.get("addressRegion"), addr.get("postalCode")) if x))
                out.setdefault("city", addr.get("addressLocality") or "")
                out.setdefault("state", addr.get("addressRegion") or "")
            elif isinstance(addr, str):
                out.setdefault("address", addr)
            out.setdefault("email", item.get("email"))
    return {k: v for k, v in out.items() if v}


def _labeled(soup: BeautifulSoup) -> dict:
    """"Phone" / "Website" / "Address" labels with the value on the next line (Houzz-style Business Details)"""
    lines = [x.strip() for x in soup.get_text("\n").split("\n") if x.strip()]
    out = {}
    for i, line in enumerate(lines[:-1]):
        key, val = line.lower().rstrip(":"), lines[i + 1]
        if val.endswith(":"):
            continue
        if key in ("business name", "company name") and len(val) > 1:
            out.setdefault("name", val)
        elif key in ("phone number", "phone", "telephone") and any(c.isdigit() for c in val):
            out.setdefault("phone", val)
        elif key in ("website", "web site") and ("." in val or "http" in val):
            out.setdefault("website", val if val.startswith("http") else "https://" + val)
        elif key in ("address", "location") and len(val) > 5:
            out.setdefault("address", val)
    return out


def tidy_name(name: str) -> str:
    """Some pages show the name twice, the trading name then the legal one: "ACS Services ACS Services LLC" -> "ACS Services LLC" """
    words = (name or "").split()
    low = [w.lower().strip(",.") for w in words]
    for k in range(2, len(words) // 2 + 1):  # two words or more, so "Smith Smith Builders" stays
        if low[:k] == low[k:2 * k]:
            return " ".join(words[k:])
    return " ".join(words)


def _constructionusa(soup: BeautifulSoup, url: str) -> dict:
    """theconstructionusa.com: the "Contact information" box, one labeled row per field"""
    out = _empty(url)
    h1 = soup.find("h1")
    out["name"] = h1.get_text(" ", strip=True) if h1 else ""
    flag = soup.select_one("img.lc-drapeau")
    if flag and flag.parent:
        out["city"], out["state"] = city_state(flag.parent.get_text(" ", strip=True))
    for label in soup.select(".lc-coord__label"):
        key, row = label.get_text(" ", strip=True).lower(), label.parent
        a = row.find("a", href=True)
        if key == "website" and a and a["href"].strip().startswith("http"):
            out["website"] = a["href"].strip()
        elif key == "email" and a:
            out["emails"] = _clean([a["href"].removeprefix("mailto:").split("?")[0] or a.get_text(strip=True)], url)
        elif key == "phone":
            out["phone"] = (a or row).get_text(" ", strip=True).removeprefix("Phone").strip()
        elif key == "address":
            out["address"] = " ".join(row.get_text(" ", strip=True).split()[1:])
    return out


def _houzz(raw: str, url: str) -> dict | None:
    """Houzz: the pro's own JSON block, matched by the URL slug so a "similar pros" block never leaks in"""
    m = re.search(r"/([^/]+)-pfvwus-pf~", url)
    hit = m and re.search(r'"titleSlug":"%s"' % re.escape(m.group(1)), raw)
    if not hit:
        return None
    back = raw[max(0, hit.start() - 2500):hit.start()]
    fwd = raw[hit.end():hit.end() + 6000]
    cut = re.search(r'"titleSlug":|"user":\{', fwd)
    fwd = fwd[:cut.start()] if cut else fwd

    def value(pattern: str, text: str) -> str:
        found = re.findall(pattern, text)
        return json.loads(f'"{found[-1]}"') if found else ""

    site = value(r'"rawDomain":"((?:[^"\\]|\\.)*)"', fwd)
    location = value(r'"location":"((?:[^"\\]|\\.)*)"', fwd)
    parts = [p.strip() for p in location.split(",")]
    out = _empty(url)
    out.update(name=value(r'"displayName":"((?:[^"\\]|\\.)*)"', back),
               website=site if not site or site.startswith(("http://", "https://")) else "http://" + site,
               phone=value(r'"formattedPhone":"((?:[^"\\]|\\.)*)"', fwd), address=location,
               city=parts[0] if len(parts) >= 2 else "", state=parts[1] if len(parts) >= 2 else "")
    return out


def _website(soup: BeautifulSoup, url: str, labeled: dict, ld: dict) -> str:
    if soup.select_one('[class*="geodir-field-"]'):
        # GeoDirectory: the "Website" field is exact, and no field means no website
        a = soup.select_one(".geodir-field-website a[href]")
        return a["href"].strip() if a and a["href"].strip().startswith("http") else ""
    if labeled.get("website"):
        return labeled["website"]
    if ld.get("website"):
        return ld["website"]
    here = urlparse(url).netloc.lower()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        host = urlparse(href).netloc.lower()
        if href.startswith("http") and _WEBSITE_LABEL.match(a.get_text(" ", strip=True)) and host and host != here \
                and not any(s in host for s in SKIP_HOSTS):
            return href
    return ""


def read(raw: str, url: str) -> dict:
    """{name, url, website, emails, phone, address, city, state} from one company page ("" where not found)"""
    out = _read(raw, url)
    out["name"] = tidy_name(out["name"])
    return out


def _read(raw: str, url: str) -> dict:
    if "theconstructionusa.com/company/" in url:
        return _constructionusa(BeautifulSoup(raw, "lxml"), url)
    if "houzz.com" in url:
        h = _houzz(raw, url)
        if h:
            return h
    soup = BeautifulSoup(raw, "lxml")
    text = soup.get_text(" ")
    labeled, ld = _labeled(soup), _json_ld(soup)
    out = _empty(url)
    h1 = soup.find("h1")
    out["name"] = (labeled.get("name") or html_lib.unescape(ld.get("name") or "").strip()
                   or (h1.get_text(" ", strip=True) if h1 else ""))
    out["website"] = _website(soup, url, labeled, ld)
    out["address"] = labeled.get("address") or ld.get("address") or ""
    phones = [labeled.get("phone") or ld.get("phone") or ""] + [a["href"][4:] for a in soup.find_all("a", href=True)
                                                                 if a["href"].lower().startswith("tel:")] + _PHONE.findall(text)
    out["phone"] = next((p.strip() for p in phones if p and 10 <= len(re.sub(r"\D", "", p)) <= 15), "")
    out["emails"] = _emails(soup, text, url)
    if ld.get("email"):
        out["emails"] = _clean(out["emails"] + [ld["email"]], url)
    city, code = city_state(out["address"])
    out["city"] = ld.get("city") or city
    out["state"] = ld.get("state") or code
    return out
