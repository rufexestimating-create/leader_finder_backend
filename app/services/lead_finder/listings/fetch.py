"""Opening directory pages. A plain request first, every redirect hop checked so a link can't point the server at its
own network; "Too Many Requests" pauses every worker; a bot-check page goes through FlareSolverr. Pages that change
only by clicking a page number are clicked through in a headless browser, for the known sites in CLICK_PAGINATION.
"""
import logging
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from app.services.lead_finder import flaresolverr, website

log = logging.getLogger("uvicorn.error")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = httpx.Timeout(20.0, connect=8.0)
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
RATE_LIMIT_WAITS = [10, 30, 60]
BLOCKED = ("client challenge", "one moment, please", "just a moment", "attention required", "checking your browser",
           "access denied", "sorry, you have been blocked", "please verify you are a human", "captcha-delivery.com",
           "datadome", "challenge-platform", "cf-browser-verification", "are you a robot", "request is being verified")


class Unsafe(Exception):
    """The address may not be opened (private network, odd port, not http)"""


def safe_url(url: str) -> str:
    """The url when the server may open it; raises Unsafe with the reason otherwise"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Unsafe("Only http and https links can be read")
    try:
        port = parts.port
    except ValueError:
        raise Unsafe("That link isn't valid")
    if port not in (None, 80, 443):
        raise Unsafe("Only links on the normal web ports can be read")
    problem = website.host_problem(parts.hostname or "")
    if problem:
        raise Unsafe(problem)
    return url


def blocked(html: str) -> bool:
    return not html or len(html) < 500 or any(s in html[:4000].lower() for s in BLOCKED)


class Fetcher:
    def __init__(self, stop=lambda: False):
        self.client = httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=False)
        self.stop = stop
        self._cool_until = 0.0
        self._lock = threading.Lock()

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait(self, seconds: float) -> bool:
        """Sleep, but wake up when the run is stopped. False when it was stopped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop():
                return False
            time.sleep(min(1.0, end - time.monotonic()))
        return True

    def _plain(self, url: str) -> tuple[int, str, str]:
        """(status, final url, html) for one GET, redirects followed by hand and each hop checked"""
        for _ in range(MAX_REDIRECTS + 1):
            safe_url(url)
            with self.client.stream("GET", url) as res:
                if res.is_redirect and res.headers.get("location"):
                    url = urljoin(url, res.headers["location"])
                    continue
                if res.status_code != 200:
                    return res.status_code, url, ""
                body = bytearray()
                for chunk in res.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= MAX_BYTES:
                        break
                return 200, str(res.url), bytes(body).decode(res.encoding or "utf-8", errors="replace")
        return 310, url, ""

    def get(self, url: str) -> tuple[str, str] | None:
        """(final url, html), or None when the page couldn't be opened. Raises Unsafe for a forbidden address."""
        limited = attempt = 0
        while attempt < 3:
            with self._lock:
                wait = self._cool_until - time.monotonic()
            if wait > 0 and not self._wait(wait):
                return None
            try:
                code, final, html = self._plain(url)
            except Unsafe:
                raise
            except httpx.HTTPError:
                attempt += 1
                if attempt < 3 and self._wait(1.0):
                    continue
                return None
            if code == 429:
                if limited >= len(RATE_LIMIT_WAITS):
                    return None
                with self._lock:
                    self._cool_until = max(self._cool_until, time.monotonic() + RATE_LIMIT_WAITS[limited])
                limited += 1
                continue
            if code == 200 and not blocked(html):
                return final, html
            if code in (403, 503) or (code == 200 and blocked(html)):
                got = flaresolverr.get(url)
                if got and not blocked(got[1]):
                    safe_url(got[0])
                    return got
                return None
            attempt += 1
            if code >= 500 and attempt < 3 and self._wait(1.0):
                continue
            return None
        return None

    def click_pages(self, url: str, selector: str, max_pages: int):
        """List pages that change only by clicking page numbers: yields each page's html, page 1 first.
        Stops at the last page, at `max_pages`, or when a click doesn't change the page."""
        from playwright.sync_api import sync_playwright

        safe_url(url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-zygote", "--disable-gpu"])
            try:
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
                ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1500)  # the site's script attaches its click handler
                n = 1
                while True:
                    yield page.content()
                    if n >= max_pages or self.stop():
                        return
                    nxt = page.locator(f'{selector}[data-page="{n + 1}"]').first
                    if nxt.count() == 0:
                        return
                    before = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href).join('|')")
                    nxt.click()
                    try:
                        page.wait_for_function(
                            """([sel, n, prev]) => {
                                const a = document.querySelector(sel + '[data-page="' + n + '"]');
                                const on = a && (a.classList.contains('is-active') || a.classList.contains('active')
                                                 || a.getAttribute('aria-current') === 'page');
                                return on && [...document.querySelectorAll('a[href]')].map(x => x.href).join('|') !== prev;
                            }""", arg=[selector, n + 1, before], timeout=45_000)
                    except Exception:
                        return
                    page.wait_for_timeout(300)
                    n += 1
            finally:
                browser.close()
