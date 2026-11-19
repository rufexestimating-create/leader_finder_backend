"""Directory list pages: which links are companies, which link is the next page, and company cards that already show
a name and website. Site-specific parsers for the directories tested so far; a generic guess for the rest.
"""
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.lead_finder.listings.email_fix import fix_email


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


# ---------- Site parsers: company links on a list page ----------

def _houzz(soup: BeautifulSoup, base: str) -> list[str]:
    skip = ("probr0-bo", "probr1-bo", "/education", "hznb", "/discussions", "/edit", "/user/", "/services")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if ("-pf~" in href or "pfvwus-" in href or "~" in href) and "/professionals/" in href \
                and not any(s in href.lower() for s in skip):
            links.append(href.split("?")[0].split("#")[0])
    return list(dict.fromkeys(links))


def _yelp(soup: BeautifulSoup, base: str) -> list[str]:
    links = []
    for a in soup.select('a[href*="/biz/"]'):
        href = urljoin(base, a["href"])
        parts = [p for p in urlparse(href).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "biz":
            links.append(href.split("?")[0])
    return list(dict.fromkeys(links))


def _yellowpages(soup: BeautifulSoup, base: str) -> list[str]:
    return list(dict.fromkeys(urljoin(base, a.get("href", "")).split("?")[0] for a in soup.select("a.business-name") if a.get("href")))


def _constructionusa(soup: BeautifulSoup, base: str) -> list[str]:
    host = urlparse(base).netloc.lower()
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("?")[0].split("#")[0]
        p = urlparse(href)
        if p.netloc.lower() == host and p.path.startswith("/company/") and len(p.path) > len("/company/"):
            links.append(href)
    return list(dict.fromkeys(links))


def _geodirectory(soup: BeautifulSoup, base: str) -> list[str]:
    """WordPress GeoDirectory sites (exchange.construction): the first profile link of each `.geodir-post` card"""
    host = urlparse(base).netloc.lower()
    list_path = urlparse(base).path.rstrip("/")
    links = []
    for card in soup.select(".geodir-loop-container .geodir-post"):
        for a in card.find_all("a", href=True):
            href = urljoin(base, a["href"]).split("#")[0].split("?")[0]
            p = urlparse(href)
            path = p.path.rstrip("/")
            if (p.netloc.lower() == host and path and path != list_path
                    and not any(s in path for s in ("/category/", "/tag/", "/author/", "/page/"))):
                links.append(href)
                break
    return list(dict.fromkeys(links))


def _generic(soup: BeautifulSoup, base: str) -> list[str]:
    """Best guess: the first same-site link of each card-like block, else every same-site link that isn't site chrome"""
    domain = urlparse(base).netloc
    links: list[str] = []
    for box in soup.select('[class*="listing"], [class*="result"], [class*="card"], [class*="business"], [class*="item"], '
                           '[class*="entry"], [class*="profile"], [class*="member"], [class*="directory"]'):
        a = box.find("a", href=True)
        if a:
            href = urljoin(base, a["href"])
            p = urlparse(href)
            if p.netloc == domain and p.path not in ("/", ""):
                links.append(href.split("#")[0])
    if len(links) < 3:
        chrome = ("/login", "/signup", "/register", "/cart", "/checkout", "/about", "/contact", "/privacy", "/terms", "/faq",
                  "/search", "/category", "/tag", ".css", ".js", ".png", ".jpg", ".pdf", "#")
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"])
            p = urlparse(href)
            if p.netloc == domain and p.path not in ("/", "") and not any(c in href.lower() for c in chrome):
                links.append(href.split("#")[0])
    return list(dict.fromkeys(links))


SITE_PARSERS = {
    "houzz.com": _houzz,
    "yelp.com": _yelp,
    "yellowpages.com": _yellowpages,
    "theconstructionusa.com": _constructionusa,
}

# List pages that change only by clicking a page number (JavaScript; ?p=2 is ignored). Value: the page-number links,
# each carrying data-page="N". Only these known public sites are opened in a browser.
CLICK_PAGINATION = {
    "theconstructionusa.com": ".lc-pagination__lien",
}


def site_parser(url: str):
    host = host_of(url)
    return next((fn for key, fn in SITE_PARSERS.items() if host == key or host.endswith("." + key)), None)


def is_geodirectory(html: str) -> bool:
    return bool(BeautifulSoup(html, "lxml").select_one(".geodir-loop-container .geodir-post"))


def listing_links(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    fn = site_parser(page_url)
    if fn:
        return fn(soup, page_url)
    if soup.select_one(".geodir-loop-container .geodir-post"):
        return _geodirectory(soup, page_url)
    return _generic(soup, page_url)


# ---------- Pages ----------

HOUZZ_PAGE_SIZE = 15


def houzz_page_url(url: str, steps_ahead: int) -> str:
    """Same Houzz URL, `steps_ahead` pages further (fi += 15 per page)"""
    p = urlparse(url)
    qs = parse_qs(p.query)
    try:
        fi = int(qs.get("fi", ["0"])[0])
    except ValueError:
        fi = 0
    qs["fi"] = [str(fi + HOUZZ_PAGE_SIZE * steps_ahead)]
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


def next_page(html: str, current: str, listings_count: int = 0, strict: bool = False) -> str | None:
    """The next list page: rel=next, a "Next" link, the link after the active page number, or ?page=N+1.
    `strict` skips the active-item guess, which can jump to an unrelated menu page."""
    soup = BeautifulSoup(html, "lxml")
    cur = urlparse(current)

    def ok(href: str) -> str | None:
        u = urljoin(current, href)
        return u if u != current else None

    for a in soup.find_all("a", href=True):
        rel = a.get("rel") or []
        if any("next" in str(r).lower() for r in (rel if isinstance(rel, list) else [rel])) and ok(a["href"]):
            return ok(a["href"])
    for a in soup.find_all("a", href=True):
        cls = " ".join(a.get("class", [])).lower()
        disabled = "disabled" in cls or a.get("disabled")
        if not disabled and "next" in (a.get("aria-label") or "").lower() and ok(a["href"]):
            return ok(a["href"])
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True).lower()
        cls = " ".join(a.get("class", [])).lower()
        if (txt in ("next", "next >", "next >>", "next »", ">", "»") or ("next" in cls and "page" in cls)) \
                and "disabled" not in cls and not a.get("disabled") and ok(a["href"]):
            return ok(a["href"])
    active = None if strict else soup.select_one('.active, .current, [aria-current="page"]')
    if active and active.parent:
        sib = active.parent.find_next_sibling()
        nxt = sib.find("a", href=True) if sib else None
        if nxt and ok(nxt["href"]):
            return ok(nxt["href"])
    qs = parse_qs(cur.query)
    for key in ("page", "p", "pg"):
        if key in qs:
            try:
                n = int(qs[key][0]) + 1
            except ValueError:
                continue
            for a in soup.find_all("a", href=True):
                if a.get_text(strip=True) == str(n):
                    return urljoin(current, a["href"])
            if listings_count >= 8:
                qs[key] = [str(n)]
                return urlunparse(cur._replace(query=urlencode(qs, doseq=True)))
    return None


# ---------- Cards: name + website straight off the list page ----------

_WEBSITE_LABEL = re.compile(r"^(visit\s+)?(our\s+)?web\s?site\b", re.I)
_STATE_ZIP = re.compile(r",\s*([A-Z]{2})\s+\d{5}")


def _contact_link(a, page_host: str) -> bool:
    href = a["href"].strip()
    if href.lower().startswith(("mailto:", "tel:")):
        return True
    return href.startswith("http") and bool(_WEBSITE_LABEL.match(a.get_text(" ", strip=True))) and urlparse(href).netloc.lower() != page_host


def cards(html: str, page_url: str) -> list[dict]:
    """Company cards on a list page (one heading + a tel:/mailto:/"Website" link each), so no profile has to be opened.
    [] when the page doesn't look like a card list with websites."""
    soup = BeautifulSoup(html, "lxml")
    page_host = urlparse(page_url).netloc.lower()
    records, shapes = [], []
    for h in soup.find_all(["h2", "h3", "h4"]):
        name = h.get_text(" ", strip=True)
        if len(name) < 2:
            continue
        card, node = None, h
        # The smallest block around the heading holding a contact link, never one that swallows a neighbour's heading
        while node.parent is not None and node.parent.name not in ("body", "html"):
            node = node.parent
            if len(node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])) > 1:
                break
            if any(_contact_link(a, page_host) for a in node.find_all("a", href=True)):
                card = node
                break
        if card is None:
            continue
        web, emails, phone, profile = "", [], "", ""
        for a in card.find_all("a", href=True):
            href, label = a["href"].strip(), a.get_text(" ", strip=True)
            low = href.lower()
            if low.startswith("mailto:"):
                em = fix_email(href[7:].split("?")[0])
                if em and em not in emails:
                    emails.append(em)
            elif low.startswith("tel:"):
                phone = phone or label or href[4:]
            elif href.startswith("http") and _WEBSITE_LABEL.match(label):
                if urlparse(href).netloc.lower() not in ("", page_host) and not web:
                    web = href
            elif not profile and href.startswith("http") and urlparse(href).netloc.lower() == page_host:
                profile = href
        if not (web or emails or phone):
            continue
        m = _STATE_ZIP.search(card.get_text(" ", strip=True))
        shapes.append((card.name, tuple(sorted(card.get("class", [])))))
        records.append({"name": name, "url": profile or page_url, "website": web, "emails": emails,
                        "state": m.group(1) if m else "", "city": "", "phone": phone, "address": ""})
    if shapes:
        # Keep only the repeated card shape (drops a footer "Get in touch" block and the like)
        common = max(set(shapes), key=shapes.count)
        records = [r for r, s in zip(records, shapes) if s == common]
    if len(records) < 3 or not any(r["website"] for r in records):
        return []
    return records
