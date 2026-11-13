"""Companies from Google Maps through gosom/google-maps-scraper (MIT, self-hosted, no API key).

The scraper runs as a one-off Docker container per search and exits when done, so its browser only uses memory while
a search is running. Only one search runs at a time on the server.
"""
import csv
import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("uvicorn.error")

IMAGE = "gosom/google-maps-scraper"
_lock = threading.Lock()


class MapsError(Exception):
    pass


def _depth(limit: int) -> int:
    # Each scroll of the results list loads roughly 10-20 more places
    return 1 if limit <= 15 else 2 if limit <= 35 else 4


def search(query: str, limit: int, timeout: int = 900) -> list[dict]:
    """[{name, website, phone, address, rating, reviews, category, maps_url}] in Google's order"""
    if not shutil.which("docker"):
        raise MapsError("Searching Google Maps needs Docker on the server, and it isn't installed.")
    with _lock, tempfile.TemporaryDirectory(prefix="gmaps-") as tmp:
        Path(tmp, "queries.txt").write_text(query.strip() + "\n", encoding="utf-8")
        cmd = ["docker", "run", "--rm", "--memory=1200m", "--cpus=0.9", "-e", "DISABLE_TELEMETRY=1",
               "-v", "gmaps-playwright-cache:/opt", "-v", f"{tmp}:/data", IMAGE,
               "-input", "/data/queries.txt", "-results", "/data/results.csv", "-depth", str(_depth(limit)),
               "-c", "1", "-exit-on-inactivity", "2m", "-lang", "en"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MapsError("Google Maps took too long to answer. Try again in a few minutes.")
        out = Path(tmp, "results.csv")
        if not out.exists():
            log.warning("gmaps scraper exited %s: %s", done.returncode, (done.stderr or done.stdout)[-400:])
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
    } for r in rows if (r.get("title") or "").strip()]
