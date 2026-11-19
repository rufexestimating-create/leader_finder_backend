"""Companies from a directory link the user pastes (theconstructionusa.com, exchange.construction, Houzz, GeoDirectory
sites, and a best guess for others). Walks the list pages, opens each company's page a few at a time and returns the
companies that have their own website, stopping as soon as there are enough new ones.

Every company comes back as {name, url, website, domain, emails, phone, address, city, state}: the agent then reads
their websites, finds and checks emails and sends, exactly as for companies found on Google Maps.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import urlsplit

from app.services.lead_finder import website
from app.services.lead_finder.listings import parsers, profile
from app.services.lead_finder.listings.fetch import Fetcher, Unsafe, safe_url

log = logging.getLogger("uvicorn.error")

MAX_PAGES = 800           # list pages; 10,000 companies at ~20 a page is 500
WORKERS = 5
HOUZZ_WORKERS = 3          # Houzz answers "Too Many Requests" above this
PAUSE = 0.4                # between list pages
BUDGET_SECONDS = 20 * 60   # a small run never spends longer than this finding companies
SECONDS_PER_COMPANY = 1.5  # bigger runs get this much more per company asked for, up to 5 hours
# Directories whose parsers were tested on the live site; the rest get the generic guess
TESTED = ("theconstructionusa.com", "exchange.construction", "houzz.com")


class ListingError(Exception):
    """The link can't give companies; the message is shown to the user"""


def _nothing_listed(url: str) -> str:
    if urlsplit(url).path.strip("/") == "":
        return (f"This is {site(url)}'s home page, which doesn't list companies. On the site, pick a category and a "
                "city, then paste the link of the page that shows the list of companies.")
    return ("No companies were found on this link. Paste the directory's list page (the one that shows many "
            "companies), not one company's page.")


def site(url: str) -> str:
    return parsers.host_of(url)


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        return safe_url(url)
    except Unsafe as e:
        raise ListingError(str(e))


def _pages(f: Fetcher, url: str, final: str, first: str):
    """(page url, html) for each list page, page 1 first"""
    host = site(url)
    selector = parsers.CLICK_PAGINATION.get(host)
    if selector:
        try:
            with closing(f.click_pages(url, selector, MAX_PAGES)) as clicked:
                yield from ((url, html) for html in clicked)
            return
        except Exception as e:  # no browser on this server: page 1 is still readable without one
            log.warning("directory click pages failed for %s: %s: %s", host, type(e).__name__, e)
            yield final, first
            return
    if host == "houzz.com" or host.endswith(".houzz.com"):
        yield final, first
        for k in range(1, MAX_PAGES):
            got = f.get(parsers.houzz_page_url(url, k))
            if not got:
                return
            yield got
        return
    seen, cur, html = {final}, final, first
    for _ in range(MAX_PAGES):
        yield cur, html
        nxt = parsers.next_page(html, cur, listings_count=len(parsers.listing_links(html, cur)), strict=True)
        if not nxt or nxt in seen:
            return
        seen.add(nxt)
        got = f.get(nxt)
        if not got:
            return
        cur, html = got


def collect(url: str, want: int, skip=lambda domain: False, progress=lambda pages, found: None,
            note=lambda text: None, stop=lambda: False) -> tuple[list[dict], dict]:
    """Up to `want` companies with a website `skip` doesn't rule out, plus {pages, profiles, listed}.
    Raises ListingError when the link can't be read or lists no companies."""
    url = check_url(url)
    deadline = time.monotonic() + min(5 * 3600, max(BUDGET_SECONDS, want * SECONDS_PER_COMPANY))
    out: list[dict] = []
    domains: set[str] = set()
    stats = {"pages": 0, "profiles": 0, "listed": 0}

    def take(rec: dict) -> None:
        d = website.clean_domain(rec.get("website") or "")
        if d and website.valid_domain(d) and d not in domains and not skip(d) and len(out) < want:
            domains.add(d)
            out.append({**rec, "domain": d})

    def done() -> bool:
        return len(out) >= want or stop() or time.monotonic() > deadline

    with Fetcher(stop) as f:
        try:
            got = f.get(url)
        except Unsafe as e:
            raise ListingError(str(e))
        if not got:
            raise ListingError("This directory couldn't be opened: it didn't answer, or it turns automated visitors away.")
        final, first = got
        host = site(url)
        use_cards = host not in parsers.CLICK_PAGINATION and not host.endswith("houzz.com") and parsers.cards(first, final)

        if use_cards:
            # The list page already shows each company's website: no company page needs opening
            for page_url, html in _pages(f, url, final, first):
                found = parsers.cards(html, page_url)
                if not found:
                    break
                stats["pages"] += 1
                stats["listed"] += len(found)
                for c in found:
                    take(c)
                note(f"Page {stats['pages']}: {len(found)} companies listed")
                progress(stats["pages"], len(out))
                if done() or not f._wait(PAUSE):
                    break
            return out, stats

        def open_profile(link: str) -> dict | None:
            try:
                page = f.get(link)
            except Unsafe:
                return None
            return profile.read(page[1], link) if page else None

        workers = HOUZZ_WORKERS if host.endswith("houzz.com") else WORKERS
        max_profiles = min(50000, max(want * 4, 60))
        seen_links: set[str] = set()
        with ThreadPoolExecutor(workers, thread_name_prefix="directory") as pool, closing(_pages(f, url, final, first)) as pages:
            for page_url, html in pages:
                new = [x for x in parsers.listing_links(html, page_url) if x not in seen_links]
                if not new:
                    break  # a page with nothing new: the list has ended
                seen_links.update(new)
                stats["pages"] += 1
                stats["listed"] += len(new)
                note(f"Page {stats['pages']}: {len(new)} companies listed, opening their pages")
                for i in range(0, len(new), workers * 2):
                    if done() or stats["profiles"] >= max_profiles:
                        break
                    batch = new[i:i + workers * 2]
                    for rec in pool.map(open_profile, batch):
                        stats["profiles"] += 1
                        if rec and rec["name"]:
                            take(rec)
                    progress(stats["pages"], len(out))
                if done() or stats["profiles"] >= max_profiles or not f._wait(PAUSE):
                    break
    if not stats["listed"]:
        raise ListingError(_nothing_listed(url))
    return out, stats


def preview(url: str) -> dict:
    """Page 1 only, for the "Check link" button: how many companies it lists and whether this directory was tested"""
    url = check_url(url)
    with Fetcher() as f:
        try:
            got = f.get(url)
        except Unsafe as e:
            raise ListingError(str(e))
    if not got:
        raise ListingError("This directory couldn't be opened: it didn't answer, or it turns automated visitors away.")
    final, html = got
    host = site(url)
    found = [] if host in parsers.CLICK_PAGINATION or host.endswith("houzz.com") else parsers.cards(html, final)
    count = len(found) or len(parsers.listing_links(html, final))
    if not count:
        raise ListingError(_nothing_listed(url))
    tested = any(host == t or host.endswith("." + t) for t in TESTED) or parsers.is_geodirectory(html)
    return {"site": host, "companies": count, "tested": tested,
            "with_website": sum(1 for c in found if c["website"]) if found else None}
