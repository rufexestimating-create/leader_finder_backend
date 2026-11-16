"""A real (headless) browser for the websites a plain request can't read, through Crawl4AI (Apache-2.0, self-hosted).

Used only as a fallback: pages built by JavaScript, "checking your browser" / robot challenge screens and sites that turn
away simple requests. One browser per agent run, started on first use and closed when the run's reading is done, so it
only uses memory while needed. Sync wrapper: the agent runs on a plain thread, so the browser gets its own event loop.
"""
import asyncio
import logging
import os

os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")

log = logging.getLogger("uvicorn.error")

PAGE_TIMEOUT_MS = 40_000
SETTLE_SECONDS = 6.0  # challenge screens solve themselves after a few seconds of JavaScript


class Browser:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._crawler = None
        self._broken = False

    def _start(self) -> bool:
        if self._crawler is not None or self._broken:
            return self._crawler is not None
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig
            self._loop = asyncio.new_event_loop()
            config = BrowserConfig(headless=True, enable_stealth=True, verbose=False,
                                   extra_args=["--disable-dev-shm-usage", "--no-zygote", "--disable-gpu"])
            self._crawler = AsyncWebCrawler(config=config)
            self._loop.run_until_complete(self._crawler.start())
            return True
        except Exception as e:  # no Chromium on this server: the plain reader carries on alone
            log.warning("browser unavailable: %s: %s", type(e).__name__, e)
            self._broken, self._crawler = True, None
            return False

    def render(self, url: str) -> tuple[str, str] | None:
        """(final url, html) after the page's JavaScript ran, or None when it still can't be read"""
        if not self._start():
            return None
        from crawl4ai import CacheMode, CrawlerRunConfig
        config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=PAGE_TIMEOUT_MS, wait_until="load",
                                  delay_before_return_html=SETTLE_SECONDS, magic=True, verbose=False)
        try:
            result = self._loop.run_until_complete(asyncio.wait_for(self._crawler.arun(url, config=config), 75))
        except Exception as e:
            log.info("browser couldn't open %s: %s: %s", url, type(e).__name__, e)
            return None
        html = result.html or ""
        if not result.success or (result.status_code or 0) >= 400 or len(html) < 500:
            return None
        return (result.redirected_url or result.url or url), html

    def close(self):
        if self._crawler is not None and self._loop is not None:
            try:
                self._loop.run_until_complete(self._crawler.close())
            except Exception:
                pass
        if self._loop is not None:
            self._loop.close()
        self._crawler, self._loop = None, None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
