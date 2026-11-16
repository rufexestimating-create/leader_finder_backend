"""FlareSolverr (MIT, self-hosted Docker service): opens a page in its own browser, waits out "checking your browser"
screens and returns the HTML. Used when a plain request is turned away. Off when FLARESOLVERR_URL is empty or the
service isn't running: callers then carry on without it.
"""
import logging
import time

import httpx

from app.core.config import settings

log = logging.getLogger("uvicorn.error")

_down_until = 0.0


def get(url: str, timeout: int = 60) -> tuple[str, str] | None:
    """(final url, html), or None when FlareSolverr is off, down or the page still can't be opened"""
    global _down_until
    if not settings.FLARESOLVERR_URL or time.monotonic() < _down_until:
        return None
    try:
        res = httpx.post(settings.FLARESOLVERR_URL, json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
                         timeout=timeout + 15)
        data = res.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _down_until = time.monotonic() + 300  # not running: don't ask again for a while
        return None
    except (httpx.HTTPError, ValueError) as e:
        log.info("flaresolverr failed for %s: %s", url, type(e).__name__)
        return None
    sol = data.get("solution") or {}
    html = sol.get("response") or ""
    if data.get("status") != "ok" or (sol.get("status") or 0) >= 400 or len(html) < 500:
        return None
    return sol.get("url") or url, html
