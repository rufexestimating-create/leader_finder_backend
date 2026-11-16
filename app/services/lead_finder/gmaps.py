"""Companies from Google Maps through gosom/google-maps-scraper (MIT, self-hosted, no API key).

The scraper runs as a one-off Docker container per search and exits when done, so its browser only uses memory while
a search is running. Only one search runs at a time on the server. With `emails` it also opens each place's website and
collects the emails on it (gosom's -email).
"""
import csv
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("uvicorn.error")

IMAGE = "gosom/google-maps-scraper"
_lock = threading.Lock()


_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}", re.I)


class MapsError(Exception):
    pass


def _depth(wanted: int) -> int:
    # Each scroll of the results list loads roughly 10-20 more places; Google stops at about 120 per search
    return 1 if wanted <= 15 else 2 if wanted <= 35 else 4 if wanted <= 60 else 8 if wanted <= 100 else 12


def _rows(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


def search(query: str | list[str], limit: int, timeout: int | None = None, emails: bool = False,
           progress=lambda places: None) -> list[dict]:
    """[{name, website, phone, address, rating, reviews, category, maps_url, emails}] in Google's order. Several
    queries ("general contractor in X", "construction company in X") reach past the ~120 places one search shows."""
    queries = [query] if isinstance(query, str) else query
    per_query = -(-limit * 3 // 2 // len(queries))  # some places have no website or are already in ReachAgent
    timeout = timeout or 900 + 300 * len(queries) + (4 * limit if emails else 0)
    if not shutil.which("docker"):
        raise MapsError("Searching Google Maps needs Docker on the server, and it isn't installed.")
    with _lock, tempfile.TemporaryDirectory(prefix="gmaps-") as tmp:
        Path(tmp, "queries.txt").write_text("".join(q.strip() + "\n" for q in queries), encoding="utf-8")
        cmd = ["docker", "run", "--rm", "--memory=1200m", "--cpus=0.9", "-e", "DISABLE_TELEMETRY=1",
               "-v", "gmaps-playwright-cache:/opt", "-v", f"{tmp}:/data", IMAGE,
               "-input", "/data/queries.txt", "-results", "/data/results.csv", "-depth", str(_depth(per_query)),
               "-c", "1", "-exit-on-inactivity", "2m", "-lang", "en"] + (["-email"] if emails else [])
        out = Path(tmp, "results.csv")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        started, told = time.monotonic(), -1
        while proc.poll() is None:  # results.csv grows as places are read: say how far it got
            if time.monotonic() - started > timeout:
                proc.kill()
                raise MapsError("Google Maps took too long to answer. Try again in a few minutes.")
            n = _rows(out)
            if n != told:
                progress(n)
                told = n
            time.sleep(10)
        err = proc.stderr.read() if proc.stderr else ""
        if not out.exists():
            log.warning("gmaps scraper exited %s: %s", proc.returncode, err[-400:])
            raise MapsError("Google Maps couldn't be searched right now. Try again in a few minutes.")
        with out.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

    def num(v, kind):
        try:
            return kind(float(v))
        except (TypeError, ValueError):
            return None

    return [{
        "name": (r.get("title") or "").strip(),
        "website": (r.get("website") or "").strip(),
        "phone": (r.get("phone") or "").strip() or None,
        "address": (r.get("address") or "").strip() or None,
        "rating": num(r.get("review_rating"), float),
        "reviews": num(r.get("review_count"), int),
        "category": (r.get("category") or "").strip(),
        "maps_url": (r.get("link") or "").strip() or None,
        "emails": list(dict.fromkeys(e.lower() for e in _EMAIL.findall(r.get("emails") or r.get("email") or ""))),
    } for r in rows if (r.get("title") or "").strip()]
