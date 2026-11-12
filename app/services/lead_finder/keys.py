"""Each user's own keys for the tools Lead Finder uses (Hunter, Serper, Reoon and the AI writer).

Keys are encrypted before they are stored and only the last four characters are ever shown again. They are checked
against the tool before they are saved, so a wrong key is caught at once and a good one shows how many credits are left.
Nothing here logs a key.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.db.supabase import get_supabase

TABLE = "prospect_api_keys"
PROVIDERS = ("hunter", "serper", "reoon", "ai", "apollo", "gplaces", "openai", "pdl")
DEFAULT_AI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
NAMES = {"hunter": "Hunter", "serper": "Serper", "reoon": "Reoon", "ai": "The AI",
         "apollo": "Apollo.io", "gplaces": "Google Places", "openai": "OpenAI", "pdl": "People Data Labs"}

log = logging.getLogger("uvicorn.error")


class KeysUnavailable(Exception):
    """Keys can't be stored or read right now (no encryption secret on the server, or the table doesn't exist yet)"""


# ---------- Encryption ----------

def _fernet() -> Fernet:
    secret = settings.KEYS_ENCRYPTION_KEY.strip()
    if not secret:
        raise KeysUnavailable("Saving keys isn't set up on the server yet: KEYS_ENCRYPTION_KEY is missing in the backend's .env file.")
    try:
        return Fernet(secret.encode())
    except ValueError:
        raise KeysUnavailable("KEYS_ENCRYPTION_KEY in the backend's .env file isn't a valid key.")


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str | None:
    """The key, or None when it was encrypted with another secret"""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


# ---------- Storage ----------

def _db():
    return get_supabase()


def _rows(user_id: str) -> list[dict]:
    try:
        return _db().table(TABLE).select("provider,key_encrypted,key_last4").eq("user_id", user_id).execute().data or []
    except Exception as e:
        log.warning("api keys could not be read: %s", type(e).__name__)
        raise KeysUnavailable("The key storage isn't ready yet: run backend/migrations/001_prospect_api_keys.sql in Supabase.")


def status(user_id: str) -> dict[str, dict]:
    """{provider: {connected, last4}} for every provider, without decrypting anything"""
    have = {r["provider"]: r["key_last4"] for r in _rows(user_id)}
    return {p: {"connected": p in have, "last4": have.get(p)} for p in PROVIDERS}


def load(user_id: str) -> dict[str, dict]:
    """{provider: {"key", "last4"}} with the keys readable, for the tools to use. Empty when nothing is saved or storage
    isn't available: the tools then simply count as not connected."""
    try:
        rows = _rows(user_id)
        out = {}
        for r in rows:
            key = decrypt(r["key_encrypted"])
            if key:
                out[r["provider"]] = {"key": key, "last4": r["key_last4"]}
        return out
    except KeysUnavailable:
        return {}


def save(user_id: str, provider: str, key: str) -> str:
    """Stores the key (encrypted) and returns its last four characters"""
    last4 = key[-4:]
    row = {"user_id": user_id, "provider": provider, "key_encrypted": encrypt(key), "key_last4": last4, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _db().table(TABLE).upsert(row, on_conflict="user_id,provider").execute()
    except Exception as e:
        log.warning("api key could not be saved: %s", type(e).__name__)
        raise KeysUnavailable("The key couldn't be saved. Run backend/migrations/001_prospect_api_keys.sql in Supabase if you haven't.")
    return last4


def remove(user_id: str, provider: str) -> None:
    try:
        _db().table(TABLE).delete().eq("user_id", user_id).eq("provider", provider).execute()
    except Exception as e:
        log.warning("api key could not be removed: %s", type(e).__name__)
        raise KeysUnavailable("The key couldn't be removed.")


# ---------- Checking a key against the tool ----------

@dataclass
class Check:
    ok: bool
    message: str


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _hunter(c: httpx.Client, key: str) -> Check:
    r = c.get("https://api.hunter.io/v2/account", headers={"X-API-KEY": key})
    if r.status_code in (401, 403):
        return Check(False, "Hunter didn't accept this key. Copy it again from hunter.io/api-keys.")
    if r.status_code != 200:
        return Check(False, f"Hunter answered with an error ({r.status_code}). Try again in a moment.")
    d = (r.json() or {}).get("data") or {}
    credits = (d.get("requests") or {}).get("credits") or {}
    left = max(0.0, _num(credits.get("available")) - _num(credits.get("used")))
    plan = d.get("plan_name") or "your"
    return Check(True, f"Connected. {plan} plan, {left:g} credits left this month.")


def _serper(c: httpx.Client, key: str) -> Check:
    r = c.get("https://google.serper.dev/account", headers={"X-API-KEY": key})
    if r.status_code in (400, 401, 403):
        return Check(False, "Serper didn't accept this key. Copy it again from serper.dev/api-key.")
    if r.status_code != 200:
        return Check(False, f"Serper answered with an error ({r.status_code}). Try again in a moment.")
    balance = int(_num((r.json() or {}).get("balance")))
    return Check(True, f"Connected. {balance:,} searches left.")


def _reoon(c: httpx.Client, key: str) -> Check:
    r = c.get("https://emailverifier.reoon.com/api/v1/check-account-balance/", params={"key": key})
    if r.status_code in (400, 401, 403):
        return Check(False, "Reoon didn't accept this key. Copy it again from your Reoon dashboard.")
    if r.status_code != 200:
        return Check(False, f"Reoon answered with an error ({r.status_code}). Try again in a moment.")
    d = r.json() or {}
    if d.get("status") != "success" or d.get("api_status") not in (None, "active"):
        return Check(False, "Reoon says this key isn't active.")
    return Check(True, f"Connected. {int(_num(d.get('remaining_daily_credits')))} daily and {int(_num(d.get('remaining_instant_credits')))} instant credits left.")


def _ai(c: httpx.Client, key: str) -> Check:
    r = c.post(
        f"{DEFAULT_AI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": DEFAULT_AI_MODEL, "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 8},
    )
    if r.status_code in (400, 401, 403):
        return Check(False, "Google didn't accept this key. Make a new one at aistudio.google.com/app/apikey.")
    if r.status_code == 429:
        return Check(False, "This key is out of free requests for now. Try again later or check its plan.")
    if r.status_code != 200:
        return Check(False, f"The AI answered with an error ({r.status_code}). Try again in a moment.")
    return Check(True, "Connected. The AI answered.")


def _apollo(c: httpx.Client, key: str) -> Check:
    hdrs = {"x-api-key": key, "Content-Type": "application/json"}
    r = c.get("https://api.apollo.io/v1/auth/health", headers=hdrs)
    if r.status_code in (401, 403):
        return Check(False, "Apollo didn't accept this key. Copy it again from app.apollo.io/#/settings/integrations/api.")
    if r.status_code != 200:
        return Check(False, f"Apollo answered with an error ({r.status_code}). Try again in a moment.")
    # A made-up person never matches, so this costs no credit; it only shows whether the plan allows the email finder
    m = c.post("https://api.apollo.io/api/v1/people/match", headers=hdrs,
               json={"first_name": "Zzqx", "last_name": "Nobodyish", "domain": "example.com"})
    if m.status_code == 403:
        return Check(False, "This key works, but Apollo's Free plan doesn't allow finding emails through the API. "
                            "Upgrade to any paid Apollo plan to use it here.")
    if m.status_code == 429:
        return Check(False, "Apollo's credits or rate limit are used up for now. Try again later.")
    # Usage numbers are only given to a master API key; a normal key gets 403 here
    u = c.post("https://api.apollo.io/api/v1/usage_stats/api_usage_stats", headers=hdrs, json={})
    if u.status_code == 200:
        stats = u.json() or {}
        match = next((v for k, v in stats.items() if "match" in k and isinstance(v, dict)), None)
        day = (match or {}).get("day") or {}
        if day.get("limit") is not None:
            left = day.get("left_over", _num(day.get("limit")) - _num(day.get("consumed")))
            return Check(True, f"Connected. {int(_num(left)):,} of {int(_num(day['limit'])):,} email lookups left today.")
    return Check(True, "Connected. To see credits here, use a master API key (app.apollo.io > Settings > Integrations > API).")


def _gplaces(c: httpx.Client, key: str) -> Check:
    r = c.get("https://maps.googleapis.com/maps/api/place/textsearch/json", params={"query": "test", "key": key})
    if r.status_code == 200:
        status = (r.json() or {}).get("status")
        if status == "REQUEST_DENIED":
            msg = (r.json() or {}).get("error_message") or "Key not authorised for Places API."
            return Check(False, f"Google Places denied the request: {msg}")
        return Check(True, "Connected. Google Places API is ready.")
    if r.status_code in (400, 403):
        return Check(False, "Google didn't accept this key. Make sure the Places API is enabled in your Google Cloud project.")
    return Check(False, f"Google Places answered with an error ({r.status_code}). Try again in a moment.")


def _openai(c: httpx.Client, key: str) -> Check:
    r = c.post(
        f"{DEFAULT_OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": DEFAULT_OPENAI_MODEL, "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 8},
    )
    if r.status_code in (401, 403):
        return Check(False, "OpenAI didn't accept this key. Copy it again from platform.openai.com/api-keys.")
    if r.status_code == 429:
        err_code = ((r.json() or {}).get("error") or {}).get("code") or ""
        if err_code == "insufficient_quota":
            return Check(False, "No billing credits on this OpenAI account. Add at least $5 at platform.openai.com/settings/billing/overview.")
        return Check(False, "OpenAI rate limit hit. Wait a moment and try again.")
    if r.status_code == 400:
        err_code = ((r.json() or {}).get("error") or {}).get("code") or ""
        if err_code == "insufficient_quota":
            return Check(False, "No billing credits on this OpenAI account. Add at least $5 at platform.openai.com/settings/billing/overview.")
        return Check(False, "OpenAI didn't accept this key. Copy it again from platform.openai.com/api-keys.")
    if r.status_code != 200:
        return Check(False, f"OpenAI answered with an error ({r.status_code}). Try again in a moment.")
    # Show remaining balance from the billing API
    bal_r = c.get(f"{DEFAULT_OPENAI_BASE_URL}/dashboard/billing/credit_grants", headers={"Authorization": f"Bearer {key}"})
    if bal_r.status_code == 200:
        grants = bal_r.json() or {}
        remaining = _num(grants.get("total_available"))
        total = _num(grants.get("total_granted"))
        if total:
            return Check(True, f"Connected. ${remaining:.2f} of ${total:.2f} credits remaining.")
    return Check(True, "Connected. OpenAI answered.")


def _pdl(c: httpx.Client, key: str) -> Check:
    # A made-up person is never found, and PDL only charges for a match; the balance comes back in the headers
    r = c.get("https://api.peopledatalabs.com/v5/person/enrich", headers={"X-Api-Key": key},
              params={"name": "Zzqx Nobodyish", "company": "nonexistentco12345"})
    if r.status_code in (401, 403):
        return Check(False, "People Data Labs didn't accept this key. Copy it again from your PDL dashboard.")
    if r.status_code == 402:
        return Check(False, "People Data Labs credits are used up. Check your plan at peopledatalabs.com.")
    if r.status_code not in (200, 404):
        return Check(False, f"People Data Labs answered with an error ({r.status_code}). Try again in a moment.")
    left = r.headers.get("x-totallimit-remaining")
    if left is not None:
        return Check(True, f"Connected. {int(_num(left)):,} credits left.")
    return Check(True, "Connected. People Data Labs is ready.")


_CHECKS = {"hunter": _hunter, "serper": _serper, "reoon": _reoon, "ai": _ai,
           "apollo": _apollo, "gplaces": _gplaces, "openai": _openai, "pdl": _pdl}


def check_key(provider: str, key: str, client: httpx.Client | None = None) -> Check:
    """Asks the tool itself whether the key works. Never raises, never logs the key."""
    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0))
    try:
        return _CHECKS[provider](c, key)
    except httpx.TimeoutException:
        return Check(False, f"{NAMES[provider]} didn't answer in time. Try again.")
    except httpx.HTTPError:
        return Check(False, f"Couldn't reach {NAMES[provider]}. Check the server's internet connection.")
    finally:
        if own:
            c.close()
