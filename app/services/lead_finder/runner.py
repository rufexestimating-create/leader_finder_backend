"""The background loop: every few seconds, start waiting companies on worker threads, within each user's limits.

Runs apart from the campaign scheduler, so a slow website never holds up replies, campaigns, follow-ups or warm-up.
Lead Finder never sends an email, so it may also run on a laptop that shares the live database (RUN_LEAD_FINDER).
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.core.config import settings as app_settings
from app.db.supabase import is_supabase_configured
from app.services.lead_finder import engine, keys, store, website
from app.services.lead_finder.providers import Apollo, GooglePlacesAPI, Hunter, PDL, ReoonVerifier, Serper
from app.services.lead_finder.writer import Writer

INTERVAL_SECONDS = 10
RECOVER_EVERY_SECONDS = 300
WORKERS = 4

_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="lead-finder")
_busy: set[str] = set()
_lock = threading.Lock()
_last_error = ""


def tools_status(user_settings: dict, user_keys: dict) -> dict:
    """For Settings: whether this user has connected each tool, and has it switched on. Serper's one key serves both
    the website search and the Google rating."""
    on = user_settings.get("tools") or {}
    has = lambda provider: bool((user_keys.get(provider) or {}).get("key"))  # noqa: E731
    connected = {
        "hunter": has("hunter"), "serper": has("serper"), "verifier": has("reoon"),
        "places": has("serper"), "ai": has("ai"),
        "apollo": has("apollo"), "gplaces": has("gplaces"), "openai": has("openai"), "pdl": has("pdl"),
    }
    return {tool: {"connected": ok, "on": bool(on.get(tool, True))} for tool, ok in connected.items()}


def build_tools(user_settings: dict, user_keys: dict, client: httpx.Client) -> engine.Tools:
    """The tools for one user's company: always that user's own keys, never anyone else's"""
    status = tools_status(user_settings, user_keys)
    use = lambda tool: status[tool]["connected"] and status[tool]["on"]  # noqa: E731
    key = lambda provider: user_keys[provider]["key"]  # noqa: E731
    return engine.Tools(
        hunter=Hunter(key("hunter"), client) if use("hunter") else None,
        serper=Serper(key("serper"), client) if use("serper") else None,
        places=Serper(key("serper"), client) if use("places") else None,
        verifier=ReoonVerifier(key("reoon"), client) if use("verifier") else None,
        writer=Writer(key("ai"), keys.DEFAULT_AI_MODEL, keys.DEFAULT_AI_BASE_URL, client) if use("ai") else None,
        read_site=lambda domain, name, city: website.read_site(client, domain, name, city),
        apollo=Apollo(key("apollo"), client) if use("apollo") else None,
        gplaces=GooglePlacesAPI(key("gplaces"), client) if use("gplaces") else None,
        openai_writer=Writer(key("openai"), keys.DEFAULT_OPENAI_MODEL, keys.DEFAULT_OPENAI_BASE_URL, client) if use("openai") else None,
        pdl=PDL(key("pdl"), client) if use("pdl") else None,
    )


def process(company: dict) -> None:
    """Runs the 8 steps for one claimed company and stores the result. Never raises."""
    cid = company["id"]
    try:
        user_settings = store.get_settings(company["user_id"])
        user_keys = keys.load(company["user_id"])
        with httpx.Client(follow_redirects=False) as client:
            tools = build_tools(user_settings, user_keys, client)
            result = engine.enrich(
                company, user_settings, tools,
                save=lambda patch: store.update_company(cid, patch),
                find_cached=lambda domain: store.find_cached(company["user_id"], domain, cid),
                now=store.now_iso,
            )
        if result.patch.get("status") == "ready":
            store.replace_contacts(company, result.contacts)
        store.update_company(cid, result.patch)
    except Exception as e:  # anything unexpected: the company fails with a plain message, the loop carries on
        print(f"[lead finder] {company.get('name')}: {type(e).__name__}: {e}", flush=True)
        try:
            store.update_company(cid, {"status": "failed", "finished_at": store.now_iso(),
                                       "error": "Something went wrong while looking into this company. Try again."})
        except Exception:
            pass
    finally:
        with _lock:
            _busy.discard(cid)


def run_due() -> int:
    """Starts as many waiting companies as there are free workers. Returns how many were started."""
    with _lock:
        free = WORKERS - len(_busy)
        busy = set(_busy)
    started = 0
    for company in store.claim(free, busy):
        with _lock:
            _busy.add(company["id"])
        _pool.submit(process, company)
        started += 1
    return started


def _log_once(message: str) -> None:
    global _last_error
    if message != _last_error:
        print(f"[lead finder] {message}", flush=True)
        _last_error = message


async def lead_finder_loop() -> None:
    if not app_settings.RUN_LEAD_FINDER:
        print("[lead finder] RUN_LEAD_FINDER=false - companies are not looked into on this copy", flush=True)
        return
    if not is_supabase_configured():
        print("[lead finder] Supabase not configured - Lead Finder disabled", flush=True)
        return
    last_recover = 0.0
    while True:
        try:
            if time.monotonic() - last_recover > RECOVER_EVERY_SECONDS:
                await asyncio.to_thread(store.recover_stuck)
                last_recover = time.monotonic()
            await asyncio.to_thread(run_due)
            _log_once("")
        except Exception as e:  # e.g. the tables don't exist yet: say it once, keep trying
            _log_once(f"{type(e).__name__}: {e}")
        await asyncio.sleep(INTERVAL_SECONDS)
