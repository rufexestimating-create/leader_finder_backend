"""Lead Finder's reads and writes in Supabase (prospect_* tables). Every user-facing call filters by user_id."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.db.supabase import get_supabase
from app.services.lead_finder.constants import DEFAULT_ROLES, DEFAULT_SETTINGS, OLD_PLACEHOLDER, is_us, naics_for
from app.services.lead_finder.engine import pending_steps
from app.services.lead_finder.website import clean_domain

COMPANIES, CONTACTS, SETTINGS, SEARCHES = "prospect_companies", "prospect_contacts", "prospect_settings", "prospect_searches"
CACHE_DAYS = 30
# A company "Finding details" for longer than this was cut off (the server restarted) and is put back in the queue
STUCK_MINUTES = 15
MAX_ATTEMPTS = 3
WAIT_NOTE = "Waiting: today's limit is reached. It starts tomorrow, or raise the limit in Settings."


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    return get_supabase()


# ---------- Settings ----------

def get_settings(user_id: str) -> dict:
    """The user's Lead Finder settings, with the defaults filling anything never saved"""
    rows = _db().table(SETTINGS).select("*").eq("user_id", user_id).limit(1).execute().data
    s = {**DEFAULT_SETTINGS, "roles": dict(DEFAULT_ROLES), "tools": dict(DEFAULT_SETTINGS["tools"])}
    if rows:
        row = rows[0]
        s["roles"].update({k: v for k, v in (row.get("roles") or {}).items() if isinstance(v, list)})
        s["tools"].update(row.get("tools") or {})
        for key in ("skip_sites", "templates", "subject", "message", "daily_limit", "at_once", "emails_per_day"):
            if row.get(key) is not None:
                s[key] = row[key]
        for key in ("agent_subject", "agent_message"):
            if (row.get(key) or "").strip():
                s[key] = row[key]
        # Saved while the message was still a placeholder: use the real message and its opening-line templates
        if OLD_PLACEHOLDER in (row.get("message") or ""):
            s.update(subject=DEFAULT_SETTINGS["subject"], message=DEFAULT_SETTINGS["message"], templates=DEFAULT_SETTINGS["templates"])
    return s


def save_settings(user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, **data, "updated_at": now_iso()}
    _db().table(SETTINGS).upsert(row, on_conflict="user_id").execute()
    return get_settings(user_id)


# ---------- Companies ----------

def company_row(user_id: str, data: dict, source: str, search_id: str | None = None) -> dict:
    """A form / CSV / search entry as a table row. US-only details are dropped for other countries,
    trades for anything but a subcontractor, and NAICS codes are filled from the type when none were given."""
    us = is_us(data.get("country"))
    company_type = data.get("company_type") or None
    trades = list(dict.fromkeys(data.get("trades") or [])) if company_type == "sub" else []
    naics = (data.get("naics") or naics_for(company_type, trades)) if us else []
    domain = clean_domain(data.get("website") or "") or None
    text = lambda k: (data.get(k) or "").strip() or None  # noqa: E731
    return {
        "user_id": user_id, "search_id": search_id, "source": source,
        "name": data["name"].strip(), "domain": domain, "country": data["country"].strip(),
        "state": text("state") if us else None, "city": data["city"].strip(), "size": data.get("size") or None,
        "company_type": company_type, "trades": trades, "contact_roles": data.get("contact_roles") or [],
        "sectors": data.get("sectors") or [], "project_types": data.get("project_types") or [],
        "revenue": text("revenue"), "project_size": text("project_size"), "founded_year": data.get("founded_year"),
        "service_area": text("service_area"), "technologies": data.get("software") or [],
        "naics": naics, "license_number": text("license_number") if us else None, "credentials": (data.get("credentials") or []) if us else [],
        "priority": data.get("priority") or None, "tags": data.get("tags") or [], "found_via": text("found_via"), "notes": text("notes"),
        "status": "queued", "steps": pending_steps(),
    }


def _with_contacts(row: dict) -> dict:
    contacts = row.pop(CONTACTS, None) or []
    row["contacts"] = sorted(contacts, key=lambda c: c.get("created_at") or "")
    return row


def list_companies(user_id: str, limit: int = 500) -> list[dict]:
    rows = (_db().table(COMPANIES).select(f"*, {CONTACTS}(*)").eq("user_id", user_id)
            .order("created_at", desc=True).limit(limit).execute().data)
    return [_with_contacts(r) for r in rows]


def get_company(user_id: str, company_id: str) -> dict | None:
    rows = _db().table(COMPANIES).select(f"*, {CONTACTS}(*)").eq("user_id", user_id).eq("id", company_id).limit(1).execute().data
    return _with_contacts(rows[0]) if rows else None


def create_companies(user_id: str, entries: list[dict], source: str, search_id: str | None = None) -> list[dict]:
    rows = [company_row(user_id, e, source, search_id) for e in entries]
    created = _db().table(COMPANIES).insert(rows).execute().data
    return [{**c, "contacts": []} for c in created]


def known_domains(user_id: str) -> set[str]:
    """Websites of every company already in Lead Finder"""
    rows = _db().table(COMPANIES).select("domain").eq("user_id", user_id).not_.is_("domain", "null").limit(5000).execute().data
    return {r["domain"] for r in rows if r.get("domain")}


def save_search(user_id: str, filters: dict, result_count: int) -> str | None:
    """Remembers what was searched, so the companies picked from it can point back to it. Never blocks a search."""
    try:
        rows = _db().table(SEARCHES).insert({"user_id": user_id, "filters": filters, "result_count": result_count}).execute().data
        return rows[0]["id"] if rows else None
    except Exception as e:  # the search itself worked; losing its history isn't worth failing it
        print(f"[lead finder] couldn't save the search: {e}", flush=True)
        return None


def delete_company(user_id: str, company_id: str) -> bool:
    return bool(_db().table(COMPANIES).delete().eq("user_id", user_id).eq("id", company_id).execute().data)


def retry_company(user_id: str, company_id: str, website: str = "") -> dict | None:
    """Back in the queue, with the website if one was typed in. What was found before is cleared."""
    company = get_company(user_id, company_id)
    if not company:
        return None
    patch = {"status": "queued", "steps": pending_steps(), "error": None, "attempts": 0, "started_at": None, "finished_at": None,
             "updated_at": now_iso()}
    if website.strip():
        patch["domain"] = clean_domain(website)
    _db().table(CONTACTS).delete().eq("company_id", company_id).execute()
    _db().table(COMPANIES).update(patch).eq("user_id", user_id).eq("id", company_id).execute()
    return get_company(user_id, company_id)


def update_contact(user_id: str, contact_id: str, patch: dict) -> dict | None:
    rows = _db().table(CONTACTS).update({**patch, "updated_at": now_iso()}).eq("user_id", user_id).eq("id", contact_id).execute().data
    return rows[0] if rows else None


# ---------- Used by the background runner ----------

def update_company(company_id: str, patch: dict) -> None:
    _db().table(COMPANIES).update({**patch, "updated_at": now_iso()}).eq("id", company_id).execute()


def replace_contacts(company: dict, contacts: list[dict]) -> None:
    db = _db()
    db.table(CONTACTS).delete().eq("company_id", company["id"]).execute()
    if contacts:
        db.table(CONTACTS).insert([{**c, "user_id": company["user_id"], "company_id": company["id"]} for c in contacts]).execute()


def find_cached(user_id: str, domain: str, exclude_id: str) -> dict | None:
    """The same website, looked into for this user in the last 30 days, with its people"""
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    rows = (_db().table(COMPANIES).select(f"*, {CONTACTS}(*)").eq("user_id", user_id).eq("domain", domain)
            .in_("status", ["ready", "approved"]).neq("id", exclude_id).gte("finished_at", since)
            .order("finished_at", desc=True).limit(1).execute().data)
    return _with_contacts(rows[0]) if rows else None


def _started_today(user_id: str) -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = _db().table(COMPANIES).select("id", count="exact").eq("user_id", user_id).gte("started_at", midnight).execute()
    return res.count or 0


def claim(max_new: int, busy: set[str]) -> list[dict]:
    """Waiting companies to start now, oldest first, keeping each user within their "at the same time" and daily
    limits. A company is only claimed if it is still waiting, so two servers never look into the same one."""
    if max_new <= 0:
        return []
    db = _db()
    waiting = db.table(COMPANIES).select("*").eq("status", "queued").order("created_at").limit(50).execute().data
    if not waiting:
        return []
    running = Counter(r["user_id"] for r in db.table(COMPANIES).select("id, user_id").eq("status", "enriching").execute().data)
    settings: dict[str, dict] = {}
    today: dict[str, int] = {}
    claimed: list[dict] = []
    for c in waiting:
        if len(claimed) >= max_new:
            break
        uid = c["user_id"]
        if c["id"] in busy:
            continue
        s = settings.setdefault(uid, get_settings(uid))
        if running[uid] >= int(s.get("at_once") or 1):
            continue
        if uid not in today:
            today[uid] = _started_today(uid)
        if today[uid] >= int(s.get("daily_limit") or 50):
            steps = c.get("steps") or pending_steps()
            if steps and steps[0].get("note") != WAIT_NOTE:
                steps[0] = {**steps[0], "note": WAIT_NOTE}
                db.table(COMPANIES).update({"steps": steps}).eq("id", c["id"]).eq("status", "queued").execute()
            continue
        steps = pending_steps()
        steps[0]["state"] = "running"
        stamp = now_iso()
        rows = (db.table(COMPANIES).update({"status": "enriching", "started_at": stamp, "finished_at": None, "error": None,
                                            "attempts": (c.get("attempts") or 0) + 1, "steps": steps, "updated_at": stamp})
                .eq("id", c["id"]).eq("status", "queued").execute().data)
        if rows:
            claimed.append(rows[0])
            running[uid] += 1
            today[uid] += 1
    return claimed


def recover_stuck() -> int:
    """After a restart: companies left "Finding details" go back in the queue, or fail after 3 tries"""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_MINUTES)).isoformat()
    db = _db()
    stuck = db.table(COMPANIES).select("id, attempts").eq("status", "enriching").lt("started_at", cutoff).execute().data
    for c in stuck:
        if (c.get("attempts") or 0) >= MAX_ATTEMPTS:
            patch = {"status": "failed", "error": "It stopped several times while being looked into. Try again later.", "finished_at": now_iso()}
        else:
            patch = {"status": "queued", "steps": pending_steps()}
        db.table(COMPANIES).update({**patch, "updated_at": now_iso()}).eq("id", c["id"]).eq("status", "enriching").execute()
    return len(stuck)
