"""Reads a company's own website and picks out what Lead Finder needs: emails, phone, social links, "since 1998",
what the site is built with, and people named next to a job title.

Safe by design, because the address comes from a form:
  * only http(s) on the normal ports, and only hosts whose every address is on the public internet (never localhost,
    the server's own network or cloud metadata), checked again on every redirect
  * robots.txt is respected
  * at most 5 pages per company, 1.5 MB and a few seconds each
Uses only the standard library's HTML parser, so no extra package is needed.
"""
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; LeadFinder/1.0)"
ROBOTS_AGENT = "LeadFinder"
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
MAX_EXTRA_PAGES = 4
PAGE_TIMEOUT = httpx.Timeout(12.0, connect=6.0)


class FetchError(Exception):
    """A page couldn't be read; the message is shown to the user"""


# ---------- Addresses ----------

_DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def clean_domain(value: str) -> str:
    """"https://www.Example.com/about?x" -> "example.com" """
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = re.split(r"[/?#]", v, maxsplit=1)[0]
    v = v.rsplit("@", 1)[-1].split(":")[0].strip(".")
    return v[4:] if v.startswith("www.") else v


def valid_domain(domain: str) -> bool:
    return bool(_DOMAIN.match(domain or ""))


def host_problem(host: str) -> str | None:
    """None when the host may be read; otherwise why not, in plain words"""
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return "The website address is empty"
    try:
        ip = ipaddress.ip_address(host)
        return None if ip.is_global else "That address points inside a private network, so it isn't read"
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return "That address points inside a private network, so it isn't read"
    infos = None
    for attempt in range(3):
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            break
        except UnicodeError:
            break
        except socket.gaierror:
            # A lookup can fail for a moment (many at once, a busy resolver) for an address that exists: ask again
            time.sleep(0.6 * (attempt + 1)) if attempt < 2 else None
    if infos is None:
        return "This website's address doesn't exist"
    addresses = {info[4][0].split("%")[0] for info in infos}
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        return "That address points inside a private network, so it isn't read"
    return None


# ---------- Fetching ----------

@dataclass
class Page:
    url: str
    text: str


def fetch(client: httpx.Client, url: str, want: str = "html") -> Page:
    """GET one page, following at most a few redirects and checking every hop. `want`: "html" or "text" (robots.txt)."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError("Only http and https websites are read")
        try:
            port = parts.port
        except ValueError:
            raise FetchError("The website address isn't valid")
        if port not in (None, 80, 443):
            raise FetchError("Only websites on the normal ports are read")
        problem = host_problem(parts.hostname or "")
        if problem:
            raise FetchError(problem)
        try:
            with client.stream("GET", url, timeout=PAGE_TIMEOUT, follow_redirects=False,
                               headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}) as res:
                if res.is_redirect:
                    location = res.headers.get("location")
                    if not location:
                        raise FetchError("The website redirects to nowhere")
                    url = urljoin(url, location)
                    continue
                if res.status_code >= 400:
                    raise FetchError(f"The website answered with error {res.status_code}")
                kind = res.headers.get("content-type", "").lower()
                if want == "html" and kind and "html" not in kind:
                    raise FetchError("That page isn't a web page")
                body = bytearray()
                for chunk in res.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= MAX_BYTES:
                        break
                return Page(str(res.url), bytes(body).decode(res.encoding or "utf-8", errors="replace"))
        except httpx.TimeoutException:
            raise FetchError("The website took too long to answer")
        except httpx.HTTPError:
            raise FetchError("The website couldn't be opened")
    raise FetchError("The website redirects too many times")


def _robots(client: httpx.Client, origin: str) -> robotparser.RobotFileParser | None:
    try:
        page = fetch(client, f"{origin}/robots.txt", want="text")
    except FetchError:
        return None  # no robots.txt: everything may be read
    rp = robotparser.RobotFileParser()
    rp.parse(page.text.splitlines())
    return rp


# ---------- Reading HTML ----------

_BLOCK = {"p", "div", "li", "td", "th", "tr", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
          "footer", "br", "figcaption", "address", "blockquote", "span", "strong", "b", "label", "small"}
# Not "head": </head> may be left out in HTML, which would hide the whole page
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}


class _Reader(HTMLParser):
    """Text split into short lines (one per block), every link with its text, JSON-LD data and the generator tag"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.generator = ""
        self._buf: list[str] = []
        self._skip = 0
        self._ld: list[str] | None = None
        self._href: str | None = None
        self._link_text: list[str] = []

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if text:
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld = []
            return
        if tag == "meta" and a.get("name", "").lower() == "generator":
            self.generator = a.get("content", "")
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "a":
            self._href, self._link_text = a.get("href", ""), []
        if tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.jsonld.append("".join(self._ld))
            self._ld = None
            return
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._skip:
            return
        self._buf.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def close(self):
        super().close()
        self._flush()


@dataclass
class Parsed:
    url: str
    html: str
    lines: list[str]
    links: list[tuple[str, str]]
    jsonld: list
    generator: str


def parse(page: Page) -> Parsed:
    r = _Reader()
    try:
        r.feed(page.text)
        r.close()
    except Exception:  # broken HTML: keep what was read
        pass
    data = []
    for raw in r.jsonld:
        try:
            data.append(json.loads(raw))
        except ValueError:
            continue
    return Parsed(page.url, page.text, r.lines, r.links, data, r.generator)


# ---------- Which pages to read ----------

# Lower = read first. Team pages name the decision makers; about and contact pages hold the rest.
_PAGE_WORDS = (("team", 0), ("leadership", 0), ("our-people", 0), ("people", 1), ("staff", 1), ("management", 1),
               ("principals", 1), ("about", 2), ("who-we-are", 2), ("company", 3), ("contact", 4), ("impressum", 4))
_FILE = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mov)$", re.I)


def _same_site(host: str, domain: str) -> bool:
    host = (host or "").lower()
    return host == domain or host.endswith("." + domain)


def find_pages(home: Parsed, domain: str, limit: int = MAX_EXTRA_PAGES) -> list[str]:
    seen, picks = {urlsplit(home.url).path.rstrip("/") or "/"}, []
    for href, text in home.links:
        if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = urljoin(home.url, href)
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        if not _same_site(parts.hostname or "", domain) or path in seen or _FILE.search(path):
            continue
        hay = f"{path.lower()} {text.lower()}".replace(" ", "-")
        score = min((s for word, s in _PAGE_WORDS if word in hay), default=None)
        if score is None:
            continue
        seen.add(path)
        picks.append((score, len(picks), f"{parts.scheme}://{parts.netloc}{parts.path}"))
    return [url for _, _, url in sorted(picks)[:limit]]


def page_name(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path.split("/")[-1].replace("-", " ").replace("_", " ").lower() if path else "home"


# ---------- Picking out details ----------

_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.[a-z]{2,24}", re.I)
_NOT_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|@2x|example\.|sentry|wixpress|domain\.com|email\.com|yourdomain|yoursite", re.I)
_PHONE_HINT = re.compile(r"\b(phone|tel|call|office|mobile|cell|ph)\b|☎|📞", re.I)
_PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,5}\)[\s.-]?)?\d{2,5}(?:[\s.-]\d{2,5}){1,3}")
_FOUNDED = re.compile(r"\b(?:since|established|founded|est\.?)\s+(?:in\s+)?((?:18|19|20)\d{2})\b", re.I)
_SOCIAL = (("linkedin.com", "LinkedIn"), ("facebook.com", "Facebook"), ("instagram.com", "Instagram"),
           ("twitter.com", "X"), ("x.com", "X"), ("youtube.com", "YouTube"), ("houzz.com", "Houzz"))
_SHARE = re.compile(r"share|intent/|sharer|/dialog/", re.I)
_TECH = (("WordPress", ("wp-content", "wp-includes")), ("Wix", ("wixstatic.com", "static.wixstatic", "wix.com")),
         ("Squarespace", ("squarespace",)), ("Shopify", ("cdn.shopify.com",)), ("Webflow", ("webflow",)),
         ("HubSpot", ("js.hs-scripts.com", "hs-analytics", "hubspot")), ("GoDaddy Website Builder", ("img1.wsimg.com",)),
         ("Weebly", ("weebly",)), ("Duda", ("multiscreensite", "dudaone")))


_CF_EMAIL = re.compile(r'data-cfemail="([0-9a-f]{8,})"|/cdn-cgi/l/email-protection#([0-9a-f]{8,})', re.I)
# "john [at] acme [dot] com", "john(at)acme(dot)com", "john at acme dot com"
_OBFUSCATED = re.compile(
    r"([a-z0-9][a-z0-9._%+-]{0,63})\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*"
    r"([a-z0-9-]+(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\.)\s*[a-z0-9-]+)+)", re.I)
_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*", re.I)


def _cloudflare(hexed: str) -> str:
    """Cloudflare hides emails as hex XOR-ed with the first byte"""
    try:
        key = int(hexed[:2], 16)
        return "".join(chr(int(hexed[i:i + 2], 16) ^ key) for i in range(2, len(hexed), 2))
    except ValueError:
        return ""


def _hidden_emails(p: Parsed) -> list[str]:
    out = [_cloudflare(a or b) for a, b in _CF_EMAIL.findall(p.html)]
    for line in p.lines:
        if len(line) > 200:
            continue
        for user, host in _OBFUSCATED.findall(line):
            out.append(f"{user}@{_DOT.sub('.', host)}")
    return out


def emails(pages: list[Parsed], domain: str) -> list[str]:
    """Every email on the pages, the company's own domain first"""
    found: list[str] = []
    for p in pages:
        sources = [h[7:].split("?")[0] for h, _ in p.links if h.lower().startswith("mailto:")] + p.lines + _hidden_emails(p)
        for text in sources:
            for m in _EMAIL.findall(text):
                e = m.lower().strip(".")
                if not _NOT_EMAIL.search(e) and e not in found:
                    found.append(e)
    return sorted(found, key=lambda e: 0 if _same_site(e.split("@")[1], domain) else 1)


def phones(pages: list[Parsed]) -> list[str]:
    """Phone numbers from tel: links first, then from lines that say phone / call / tel"""
    out: list[str] = []

    def add(raw: str):
        number = " ".join(raw.split()).strip(" .-")
        digits = re.sub(r"\D", "", number)
        if 9 <= len(digits) <= 15 and all(re.sub(r"\D", "", o) != digits for o in out):
            out.append(number)

    for p in pages:
        for href, text in p.links:
            if href.lower().startswith("tel:"):
                add(text if re.search(r"\d{3}", text) else href[4:])
    for p in pages:
        for line in p.lines:
            if len(line) < 120 and _PHONE_HINT.search(line):
                for m in _PHONE.findall(line):
                    add(m)
    return out


def socials(pages: list[Parsed]) -> list[dict]:
    out: dict[str, str] = {}
    for p in pages:
        for href, _ in p.links:
            parts = urlsplit(href)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or _SHARE.search(href):
                continue
            for site, label in _SOCIAL:
                if (host == site or host.endswith("." + site)) and label not in out and parts.path.strip("/"):
                    out[label] = href
    return [{"label": k, "url": v} for k, v in out.items()]


def founded_year(pages: list[Parsed]) -> int | None:
    this_year = date.today().year
    for p in pages:
        for item in _ld_items(p.jsonld):
            year = str(item.get("foundingDate") or "")[:4]
            if year.isdigit() and 1800 <= int(year) <= this_year:
                return int(year)
        for line in p.lines:
            m = _FOUNDED.search(line)
            if not m or not 1800 <= int(m.group(1)) <= this_year or len(line) > 160:
                continue
            # "Since 2007, we've delivered projects across India" is about one office. Only a short "since" line
            # ("Building Dallas since 2004") is about the company itself.
            if m.group(0).lower().startswith("since") and len(line) > 60:
                continue
            return int(m.group(1))
    return None


def technologies(pages: list[Parsed]) -> list[str]:
    out: list[str] = []
    for p in pages:
        hay = (p.html[:400_000] + " " + p.generator).lower()
        for name, signs in _TECH:
            if name not in out and any(s in hay for s in signs):
                out.append(name)
    return out


# ---------- People named next to a job title ----------

_TITLE_WORDS = ("owner", "co-owner", "founder", "co-founder", "ceo", "chief executive", "president", "vice president", "vp",
                "principal", "partner", "managing partner", "director", "managing director", "estimator", "chief estimator",
                "preconstruction", "project manager", "operations manager", "general manager", "superintendent",
                "project architect", "architect", "engineer", "designer", "coo", "cfo", "cto", "chairman", "proprietor")
_TITLE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(w) for w in sorted(_TITLE_WORDS, key=len, reverse=True)) + r")(?![a-z])", re.I)
_NAME = re.compile(r"\b([A-Z][a-zA-Z'’-]{1,20}(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-zA-Z'’-]{1,24}){1,2})\b")
_NOT_NAME = {
    "our", "team", "meet", "the", "about", "contact", "us", "company", "services", "service", "projects", "project", "home",
    "read", "more", "learn", "view", "inc", "llc", "ltd", "co", "corp", "group", "construction", "builders", "building",
    "design", "designs", "architects", "architecture", "engineering", "street", "suite", "road", "avenue", "manager",
    "director", "president", "owner", "founder", "principal", "partner", "estimator", "senior", "chief", "vice", "executive",
    "officer", "general", "operations", "managing", "sales", "marketing", "office", "phone", "email", "call", "today", "free",
    "quote", "pvt", "limited", "private", "our", "leadership", "management", "staff", "people", "welcome", "and", "of",
    "new", "york", "united", "states", "estimating", "preconstruction", "development", "business", "customer", "client",
    # Headings and phrases that look like names next to a title word ("Trusted Local Partner", "Site Work Principal")
    "trusted", "local", "site", "work", "commercial", "residential", "real", "estate", "success", "start", "explore", "careers",
    "employee", "stories", "simulation", "family", "homes", "home", "remodeling", "renovation", "roofing", "plumbing",
    "electrical", "concrete", "custom", "quality", "best", "top", "award", "awards", "years", "experience", "skip", "main",
    "content", "menu", "click", "here", "partners", "clients", "testimonials", "reviews", "review", "river", "oaks",
}
# Lines that quote a customer, not someone who works there
_REVIEWER = re.compile(r"\b(home\s?owner|homeowner|customer|client|review|testimonial|satisfied)\b", re.I)


def _name_in(text: str, exclude: set[str]) -> str | None:
    for m in _NAME.finditer(text):
        name = " ".join(m.group(1).split())
        words = [w.strip(".").lower() for w in name.split()]
        if any(w in _NOT_NAME or w in exclude for w in words) or len(name) > 40:
            continue
        # "EMPLOYEE STORIES", "AVT Simulation": a real name isn't shouted in capitals
        if any(len(w) > 3 and w.isupper() for w in name.replace(".", "").split()):
            continue
        return name
    return None


def clean_title(title: str) -> str:
    """'Founder of RJT Construction | Houston, TX Skip to main content' -> 'Founder of RJT Construction'"""
    title = re.split(r"\s[|•·]\s|\bskip to\b", title, maxsplit=1, flags=re.I)[0]
    return title.strip(" ,|–-:;()")[:80]


def _ld_items(data) -> list[dict]:
    """Every object in JSON-LD, including @graph and nested employee / founder lists"""
    out, stack = [], [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            out.append(item)
            for key in ("@graph", "employee", "employees", "founder", "founders", "member", "members"):
                if key in item:
                    stack.append(item[key])
    return out


def people(pages: list[Parsed], company_name: str = "", city: str = "") -> list[dict]:
    """[{name, title, source}] for people the website names next to a job title"""
    exclude = {w.lower() for w in re.findall(r"[A-Za-z]+", f"{company_name} {city}")}
    found: dict[str, dict] = {}

    def add(name: str, title: str, url: str):
        key = name.lower()
        if key not in found and not _REVIEWER.search(title):
            found[key] = {"name": name, "title": clean_title(title), "source": url}

    for p in pages:
        for item in _ld_items(p.jsonld):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and isinstance(item.get("name"), str) and len(item["name"].split()) >= 2:
                add(item["name"].strip(), str(item.get("jobTitle") or ""), p.url)
        for i, line in enumerate(p.lines):
            if len(line) > 120:
                continue
            m = _TITLE.search(line)
            if not m:
                continue
            # "Daniel Brooks, President" / "President: Daniel Brooks" / "Daniel Brooks – President"
            rest = line[: m.start()] + " " + line[m.end():]
            name = _name_in(rest, exclude)
            title = line.replace(name, "") if name else line
            # A name on its own line just above the title line
            if not name and i > 0 and len(p.lines[i - 1]) <= 60 and not _TITLE.search(p.lines[i - 1]):
                name, title = _name_in(p.lines[i - 1], exclude), line
            if name:
                add(name, title if len(title) <= 80 else m.group(1).title(), p.url)
    return list(found.values())


def email_for_person(found: list[str], name: str, domain: str) -> str | None:
    """An email on the website that is clearly this person's: first@, first.last@, flast@…"""
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split()]
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        return None
    first, last = parts[0], parts[-1]
    wanted = {first, f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}", f"{first}_{last}", f"{first[0]}.{last}"}
    for e in found:
        local, _, host = e.partition("@")
        if _same_site(host, domain) and local in wanted:
            return e
    return None


# ---------- Picking the company's website from search results ----------

_GENERIC = {"the", "and", "of", "co", "company", "inc", "llc", "ltd", "pvt", "private", "limited", "group", "corp",
            "corporation", "services", "service", "firm"}


def name_tokens(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC and len(w) >= 2]


def matches_name(host: str, company_name: str) -> bool:
    """lonestarbuilders.com matches "Lone Star Builders": enough of the name's words are in the address"""
    tokens = name_tokens(company_name)
    if not tokens:
        return False
    label = re.sub(r"[^a-z0-9]", "", clean_domain(host).rsplit(".", 1)[0])
    hits = sum(1 for t in tokens if t in label)
    return hits >= min(2, len(tokens))


def skipped(host: str, skip_sites: list[str]) -> bool:
    host = clean_domain(host)
    return any(host == s or host.endswith("." + s) for s in (clean_domain(x) for x in skip_sites) if s)


def pick_website(results: list[dict], company_name: str, skip_sites: list[str]) -> tuple[str, str] | None:
    """(domain, the result's link) for the first result that is the company's own site, not a directory"""
    for r in results:
        host = clean_domain(r.get("link") or "")
        if host and valid_domain(host) and not skipped(host, skip_sites) and matches_name(host, company_name):
            return host, r["link"]
    return None


# ---------- What the company says about itself ----------

_BOILERPLATE = re.compile(r"cookie|privacy|terms|copyright|©|all rights reserved|subscribe|newsletter|log ?in|sign ?in|javascript|"
                          r"skip to|menu|click here|read more|learn more|get a quote|request a|call us|follow us|powered by", re.I)
_PAGE_ORDER = {"about": 0, "about us": 0, "who we are": 0, "home": 1}


def excerpts(parsed: list, names: list[str], per_page: int = 5, total: int = 10) -> list[dict]:
    """Sentences from the home and about pages that say what the company does: [{page, text}]. Short menu items, cookie
    notices and legal lines are left out; each sentence appears once."""
    out: list[dict] = []
    seen: set[str] = set()
    for i in sorted(range(len(parsed)), key=lambda i: (_PAGE_ORDER.get(names[i], 2), i)):
        taken = 0
        for line in parsed[i].lines:
            key = line.lower()
            if (taken >= per_page or len(out) >= total or not 50 <= len(line) <= 280 or len(line.split()) < 8
                    or _BOILERPLATE.search(line) or key in seen or "@" in line
                    or (len(line) < 90 and not re.search(r"[.!?]$", line) and _TITLE.search(line) and not re.search(r"\d", line))):
                continue
            seen.add(key)
            out.append({"page": names[i], "text": line})
            taken += 1
    return out


# ---------- Everything for one website ----------

@dataclass
class SiteData:
    pages: list[str] = field(default_factory=list)  # "home", "team", "about"…
    page_urls: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: list[dict] = field(default_factory=list)
    founded_year: int | None = None
    technologies: list[str] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    excerpts: list[dict] = field(default_factory=list)  # [{page, text}]: sentences about what the company does
    error: str | None = None


_TRY_AGAIN = ("The website took too long to answer", "The website couldn't be opened", "This website's address doesn't exist")


def read_site(client: httpx.Client, domain: str, company_name: str = "", city: str = "") -> SiteData:
    """Home page plus up to 4 team / about / contact pages. Never raises: a site that can't be read sets `error`."""
    data = SiteData()
    if not valid_domain(domain):
        data.error = "The website address isn't valid"
        return data
    home = None
    errors: list[str] = []
    for base in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        try:
            home = fetch(client, base)
            break
        except FetchError as e:
            errors.append(str(e))
    if home is None and errors and errors[0] in _TRY_AGAIN:
        # Slow or briefly unreachable: one more try at the address as given
        time.sleep(1.5)
        try:
            home = fetch(client, f"https://{domain}/")
        except FetchError as e:
            errors[0] = str(e)
    if home is None:
        # The first try (the address as given) says what really went wrong; the www. and http:// fallbacks don't
        data.error = errors[0] if errors else "The website couldn't be opened"
        return data
    data.error = None
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(home.url))
    rules = _robots(client, origin)
    if rules is not None and not rules.can_fetch(ROBOTS_AGENT, home.url):
        data.error = "The website asks tools like this one not to read it"
        return data

    parsed = [parse(home)]
    names, urls = ["home"], [home.url]
    for url in find_pages(parsed[0], domain):
        if rules is not None and not rules.can_fetch(ROBOTS_AGENT, url):
            continue
        try:
            page = fetch(client, url)
        except FetchError:
            continue
        parsed.append(parse(page))
        names.append(page_name(url))
        urls.append(page.url)

    data.pages, data.page_urls = names, urls
    data.emails = emails(parsed, domain)
    data.phones = phones(parsed)
    data.socials = socials(parsed)
    data.founded_year = founded_year(parsed)
    data.technologies = technologies(parsed)
    data.people = people(parsed, company_name, city)
    data.excerpts = excerpts(parsed, names)
    return data
