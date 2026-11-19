"""Companies from Google Maps through gosom/google-maps-scraper (MIT, self-hosted, no API key).

The scraper runs as a one-off Docker container per search and exits when done, so its browser only uses memory while
a search is running. Up to MAX_PARALLEL_SCANS searches run at a time on the server. With `emails` it also opens each place's website and
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
import uuid
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("uvicorn.error")

IMAGE = "gosom/google-maps-scraper"
_lock = threading.BoundedSemaphore(max(1, settings.MAX_PARALLEL_SCANS))


_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}", re.I)


class MapsError(Exception):
    pass


STALL_SECONDS = 180  # no new place for this long: the search is over even if the scraper hasn't exited


def _stop(name: str, proc: subprocess.Popen) -> None:
    """Stop the scraper's container (killing only the docker CLI would leave it running) and reap the CLI"""
    try:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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
        name = f"gmaps-{uuid.uuid4().hex[:12]}"  # named, so the container itself can be stopped, not only the CLI
        cmd = ["docker", "run", "--rm", "--name", name, "--memory=1200m", "--cpus=0.9", "-e", "DISABLE_TELEMETRY=1",
               "-v", "gmaps-playwright-cache:/opt", "-v", f"{tmp}:/data", IMAGE,
               "-input", "/data/queries.txt", "-results", "/data/results.csv", "-depth", str(_depth(per_query)),
               "-c", "1", "-exit-on-inactivity", "2m", "-lang", "en"] + (["-email"] if emails else [])
        out = Path(tmp, "results.csv")
        enough = per_query * len(queries)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        started = grew = time.monotonic()
        told, why = -1, ""
        try:
            while proc.poll() is None:  # results.csv grows as places are read: say how far it got
                n = _rows(out)
                if n != told:
                    progress(n)
                    told, grew = n, time.monotonic()
                # The scraper sometimes keeps its browser open after the last place: don't wait on it for nothing
                if n >= enough:
                    why = "enough"
                elif n and time.monotonic() - grew > STALL_SECONDS:
                    why = "stalled"
                elif time.monotonic() - started > timeout:
                    why = "timeout"
                if why:
                    log.info("gmaps: stopping the scraper (%s) with %s places", why, n)
                    break
                time.sleep(10)
        finally:
            if proc.poll() is None:
                _stop(name, proc)
        err = proc.stderr.read() if proc.stderr else ""
        if why == "timeout" and not _rows(out):
            raise MapsError("Google Maps took too long to answer. Try again in a few minutes.")
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
