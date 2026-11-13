"""ReachAgent's automatic run "without enrichment": no API key, no paid tool.

  1 find    Google Maps (gosom) for "<company type> in <city>": name, website, phone, address, rating
  2 save    new companies only (a website already in ReachAgent is skipped)
  3 read    each company's website: emails, phone, social links, the people it names
  4 emails  one address per company: a named person's email on the site, else a named person's email guessed and
            confirmed by the company's own mail server, else the company address (info@, office@)
  5 send    the agent message from Settings, one email at a time with a gap, within the daily limit and test mode

Steps 1-4 run on their own thread, one run at a time on the server. Sending is done one email per tick, so a run can
be stopped at any moment and carries on after a restart or the next day.
"""
import logging
import random
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from app.db.supabase import get_supabase
from app.services.lead_finder import gmaps, mailbox, mailcheck, store, website
from app.services.lead_finder.constants import ALWAYS_SKIP, STEP_IDS, is_us, type_label

log = logging.getLogger("uvicorn.error")

RUNS = "prospect_agent_runs"
STEPS = [("find", "Find companies on Google Maps"), ("save", "Save new companies"), ("read", "Read their websites"),
         ("emails", "Find an email for each"), ("send", "Send the message")]
TEST_GAP = (10, 20)       # seconds between test emails (they all go to you)
REAL_GAP = (60, 180)      # seconds between real emails
BOUNCE_LIMIT = 3
_LEAD = ("owner", "founder", "president", "ceo", "principal", "partner", "managing")

_thread: threading.Thread | None = None


def _db():
    return get_supabase()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Runs ----------

def new_steps() -> list[dict]:
    return [{"id": i, "label": label, "state": "pending", "note": ""} for i, label in STEPS]


def start(user_id: str, filters: dict) -> dict:
    row = {"user_id": user_id, "mode": filters.get("mode", "fast"), "filters": filters, "status": "queued",
           "steps": new_steps(), "counts": {}, "processed": []}
    return _db().table(RUNS).insert(row).execute().data[0]


def list_runs(user_id: str, limit: int = 10) -> list[dict]:
    return (_db().table(RUNS).select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute().data)


def get(user_id: str, run_id: str) -> dict | None:
    rows = _db().table(RUNS).select("*").eq("user_id", user_id).eq("id", run_id).limit(1).execute().data
    if not rows:
        return None
    companies = (_db().table(store.COMPANIES).select(f"*, {store.CONTACTS}(*)").eq("user_id", user_id)
                 .eq("agent_run_id", run_id).order("created_at").execute().data)
    return {**rows[0], "companies": [store._with_contacts(c) for c in companies]}


def stop(user_id: str, run_id: str) -> dict | None:
    rows = (_db().table(RUNS).update({"status": "stopped", "finished_at": _now().isoformat(), "updated_at": _now().isoformat()})
            .eq("user_id", user_id).eq("id", run_id).in_("status", ["queued", "running", "sending", "waiting"]).execute().data)
    return rows[0] if rows else None


def _save(run_id: str, patch: dict) -> None:
    _db().table(RUNS).update({**patch, "updated_at": _now().isoformat()}).eq("id", run_id).execute()


def _status(run_id: str) -> str:
    rows = _db().table(RUNS).select("status").eq("id", run_id).limit(1).execute().data
    return rows[0]["status"] if rows else "stopped"


class _Run:
    """The live state of one run: every change is written straight away so the screen can follow it"""

    def __init__(self, row: dict):
        self.id = row["id"]
        self.steps = row.get("steps") or new_steps()
        self.counts = dict(row.get("counts") or {})

    def step(self, sid: str, state: str, note: str = ""):
        for s in self.steps:
            if s["id"] == sid:
                s["state"], s["note"] = state, note or s.get("note", "")
        _save(self.id, {"steps": self.steps, "counts": self.counts})

    def count(self, **kv):
        self.counts.update(kv)
        _save(self.id, {"counts": self.counts})

    def stopped(self) -> bool:
        return _status(self.id) == "stopped"


# ---------- Steps 1-4: find, save, read, emails ----------

def _query(f: dict) -> str:
    place = ", ".join(x for x in (f.get("city"), f.get("state"), None if is_us(f.get("country")) else f.get("country")) if x)
    return f"{type_label(f['company_type']).lower()} in {place}"


def _lead_first(people: list[dict]) -> list[dict]:
    return sorted(people, key=lambda p: 0 if any(w in (p.get("title") or "").lower() for w in _LEAD) else 1)


def _pick_contact(company: dict, domain: str, site: website.SiteData) -> tuple[dict | None, str]:
    """The one person (or company address) to email, and a note saying how it was found"""
    own = [e for e in site.emails if website._same_site(e.split("@")[1], domain)]
    free = [e for e in site.emails if e.split("@")[1] in mailcheck.FREE_MAIL]
    people = _lead_first(site.people)

    def person(p: dict, email: str, status: str, source: str) -> dict:
        return {"name": p["name"], "title": p.get("title") or None, "email": email, "email_status": status,
                "email_source": source, "verified_at": store.now_iso() if status == "valid" else None}

    for e in own:
        p = mailcheck.owner_of(e, people, domain)
        if p:
            return person(p, e, "unknown", "Website"), f"{p['name']}'s email is on the website"
    note = ""
    if people:
        d = mailcheck.check_domain(domain)
        if d.verifiable:
            pattern = mailcheck.learn_pattern(own, people, domain)
            for p in people[:2]:
                e = mailcheck.find_email(d, p["name"], domain, pattern)
                if e:
                    return (person(p, e, "valid", "Guessed from the name, confirmed by their mail server"),
                            f"{p['name']}'s email confirmed by their mail server")
            note = f"{people[0]['name']}'s email couldn't be confirmed"
        else:
            note = d.reason
    company_email = next((e for e in own if mailcheck.is_role(e)), own[0] if own else (free[0] if free else None))
    if company_email:
        contact = {"name": company["name"], "title": "Company email", "email": company_email, "email_status": "unknown",
                   "email_source": "Website", "verified_at": None}
        return contact, f"Company email from the website{f'. {note}' if note else ''}"
    return None, note or "No email on the website"


def _company_steps(domain: str, site: website.SiteData, contact: dict | None, how: str) -> list[dict]:
    notes = {
        "website": f"Found on Google Maps: {domain}",
        "cache": "Agent run without enrichment: nothing looked up in paid tools",
        "read": f"Read {len(site.pages)} page(s)" if site.pages else f"Couldn't read the website ({site.error})",
        "extract": f"{len(site.emails)} email(s), {len(site.phones)} phone(s), {len(site.socials)} social link(s)",
        "people": ", ".join(f"{p['name']} ({p.get('title') or '?'})" for p in site.people[:3]) or "The website names no one",
        "emails": how,
        "verify": "Confirmed by their mail server" if contact and contact["email_status"] == "valid" else "Not checked",
        "line": "The agent message from Settings (no personal line)",
    }
    return [{"id": s, "state": "done", "note": notes[s]} for s in STEP_IDS]


def _search(row: dict) -> None:
    run, f, uid = _Run(row), row["filters"], row["user_id"]
    try:
        settings = store.get_settings(uid)
        run.step("find", "running", f"Searching “{_query(f)}”")
        places = gmaps.search(_query(f), int(f.get("limit") or 20))
        if run.stopped():
            return
        skip = list(settings.get("skip_sites") or []) + list(ALWAYS_SKIP)
        known, picked = store.known_domains(uid), []
        for p in places:
            d = website.clean_domain(p["website"])
            if not d or not website.valid_domain(d) or website.skipped(d, skip) or "association" in p["category"].lower():
                continue
            if d in known or any(x["domain"] == d for x in picked):
                continue
            picked.append({**p, "domain": d})
        picked = picked[: int(f.get("limit") or 20)]
        run.count(found=len(places), new=len(picked))
        run.step("find", "done", f"{len(places)} on Google Maps, {len(picked)} new with a website")
        if not picked:
            for sid in ("save", "read", "emails", "send"):
                run.step(sid, "done", "Nothing new to do")
            _save(run.id, {"status": "done", "finished_at": _now().isoformat()})
            return

        run.step("save", "running")
        rows = []
        for p in picked:
            r = store.company_row(uid, {"name": p["name"], "website": p["domain"], "country": f["country"], "state": f.get("state"),
                                        "city": f["city"], "company_type": f["company_type"]}, "search")
            r.update(status="enriching", phone=p["phone"], address=p["address"], agent_run_id=run.id,
                     rating=round(p["rating"], 1) if p["rating"] else None, reviews=p["reviews"],
                     sources=[{"label": "Google Maps", "url": p["maps_url"]}] if p["maps_url"] else [])
            rows.append(r)
        saved = _db().table(store.COMPANIES).insert(rows).execute().data
        run.step("save", "done", f"{len(saved)} companies saved")

        run.step("read", "running", f"0 of {len(saved)}")
        run.step("emails", "running")
        with_email = 0
        with httpx.Client(follow_redirects=False) as client:
            for i, c in enumerate(saved, 1):
                if run.stopped():
                    return
                site = website.read_site(client, c["domain"], c["name"], c["city"])
                contact, how = _pick_contact(c, c["domain"], site)
                if contact:
                    with_email += 1
                    store.replace_contacts(c, [{**contact, "linkedin": None, "personal_line": "", "selected": True}])
                store.update_company(c["id"], {
                    "status": "ready", "finished_at": store.now_iso(), "error": None,
                    "steps": _company_steps(c["domain"], site, contact, how),
                    "phone": c.get("phone") or (site.phones[0] if site.phones else None),
                    "socials": site.socials, "founded_year": site.founded_year, "technologies": site.technologies,
                })
                run.count(read=i, emails=with_email)
                run.step("read", "running", f"{i} of {len(saved)}")
                run.step("emails", "running", f"{with_email} found so far")
        run.step("read", "done", f"{len(saved)} websites read")
        run.step("emails", "done", f"{with_email} of {len(saved)} companies have an email")
        run.step("send", "running", "Starting")
        _save(run.id, {"status": "sending", "next_send_at": _now().isoformat()})
    except gmaps.MapsError as e:
        run.step("find", "failed", str(e))
        _save(run.id, {"status": "failed", "error": str(e), "finished_at": _now().isoformat()})
    except Exception as e:  # anything unexpected: the run fails with a plain message, the server carries on
        log.warning("agent run %s failed: %s: %s", run.id, type(e).__name__, e)
        _save(run.id, {"status": "failed", "error": "Something went wrong during this run. Try again.", "finished_at": _now().isoformat()})


# ---------- Step 5: send, one email per tick ----------

def render(settings: dict, company: dict, contact: dict) -> tuple[str, str]:
    first = contact["name"].split()[0] if contact.get("title") != "Company email" else ""
    greeting = f"Hi {first}," if first else f"Hi {company['name']} team,"

    def fill(text: str) -> str:
        for key, value in (("greeting", greeting), ("company", company["name"]), ("first_name", first or "there"),
                           ("city", company.get("city") or "")):
            text = re.sub(r"\{\{\s*" + key + r"\s*\}\}", lambda _, v=value: v, text)
        return text

    return fill(settings.get("agent_subject") or ""), fill(settings.get("agent_message") or "").strip()


def _sent_today(user_id: str) -> int:
    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = _db().table(store.CONTACTS).select("id", count="exact").eq("user_id", user_id).gte("sent_at", midnight).execute()
    return res.count or 0


def _emailed_before(user_id: str, email: str) -> bool:
    return bool(_db().table(store.CONTACTS).select("id").eq("user_id", user_id).eq("email", email)
                .not_.is_("sent_at", "null").limit(1).execute().data)


def _bounces_since(mb: dict, since: datetime) -> int:
    try:
        with mailbox._imap(mb) as m:
            m.select("INBOX", readonly=True)
            day = since.strftime("%d-%b-%Y")
            total = 0
            for sender in ("mailer-daemon", "postmaster"):
                _, data = m.search(None, "SINCE", day, "FROM", sender)
                total += len(data[0].split()) if data and data[0] else 0
            return total
    except Exception:
        return 0


def _finish(run: dict, status: str, note: str, error: str | None = None) -> None:
    steps = run.get("steps") or new_steps()
    for s in steps:
        if s["id"] == "send":
            s["state"], s["note"] = ("done" if status == "done" else "failed"), note
    _save(run["id"], {"status": status, "steps": steps, "error": error, "finished_at": _now().isoformat()})


def _send_one(run: dict) -> None:
    uid, rid = run["user_id"], run["id"]
    mb = mailbox.load(uid)
    if not mb:
        return _finish(run, "failed", "No mailbox connected", "Connect your mailbox in Settings, then start a new run.")
    settings = store.get_settings(uid)
    test = mailbox.is_test(mb)
    counts, processed = dict(run.get("counts") or {}), list(run.get("processed") or [])
    steps = run.get("steps") or new_steps()

    def set_send_note(note: str):
        for s in steps:
            if s["id"] == "send":
                s["state"], s["note"] = "running", note

    if not test and _sent_today(uid) >= int(settings.get("emails_per_day") or 30):
        tomorrow = (_now() + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        set_send_note(f"Today's limit of {settings.get('emails_per_day')} emails is reached: the rest go tomorrow")
        return _save(rid, {"status": "waiting", "steps": steps, "next_send_at": tomorrow.isoformat()})

    companies = (_db().table(store.COMPANIES).select(f"*, {store.CONTACTS}(*)").eq("user_id", uid).eq("agent_run_id", rid)
                 .order("created_at").execute().data)
    todo = [(c, p) for c in companies for p in (c.get(store.CONTACTS) or [])
            if p.get("selected") and p.get("email") and not p.get("sent_at") and p["id"] not in processed]
    if not todo:
        sent = counts.get("sent", 0)
        return _finish(run, "done", f"{sent} email{'' if sent == 1 else 's'} sent{' (test mode: all to you)' if test else ''}")
    company, contact = todo[0]
    processed.append(contact["id"])

    if not test and _emailed_before(uid, contact["email"]):
        store.update_contact(uid, contact["id"], {"send_error": "Already emailed before"})
        counts["skipped"] = counts.get("skipped", 0) + 1
        return _save(rid, {"processed": processed, "counts": counts})

    subject, body = render(settings, company, contact)
    if test:
        note = f"TEST MODE: with test mode off, this email goes to {contact['name']} <{contact['email']}>.\n\n"
        msg = mailbox.build(mb, mb["email"], mb.get("from_name") or "", f"[Test] {subject}", note + body)
    else:
        msg = mailbox.build(mb, contact["email"], contact["name"] if contact.get("title") != "Company email" else "", subject, body)
    try:
        error = mailbox.send(mb, [msg], copy_to_sent=not test)[0]
    except mailbox.MailboxError as e:
        return _finish(run, "failed", "Your mailbox couldn't send", str(e))

    if error:
        store.update_contact(uid, contact["id"], {"send_error": error})
        counts["failed"] = counts.get("failed", 0) + 1
    else:
        counts["sent"] = counts.get("sent", 0) + 1
        if not test:
            stamp = store.now_iso()
            store.update_contact(uid, contact["id"], {"sent_at": stamp, "approved_at": stamp, "send_error": None,
                                                      "message_id": msg["Message-ID"]})
            store.update_company(company["id"], {"status": "approved"})
    left = len(todo) - 1
    set_send_note(f"{counts.get('sent', 0)} sent, {left} to go" + (" (test mode: all to you)" if test else ""))
    gap = random.randint(*(TEST_GAP if test else REAL_GAP))
    patch = {"processed": processed, "counts": counts, "steps": steps, "next_send_at": (_now() + timedelta(seconds=gap)).isoformat()}

    if not test and counts.get("sent", 0) % 5 == 0:
        started = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if _bounces_since(mb, started) >= BOUNCE_LIMIT:
            _save(rid, patch)
            return _finish({**run, "steps": steps}, "stopped", "Stopped: several emails bounced",
                           f"{BOUNCE_LIMIT} or more emails bounced, so the run stopped to protect your mailbox. Check your inbox.")
    _save(rid, patch)


# ---------- The loop's tick ----------

def recover() -> None:
    """After a restart a search that was cut off starts again (companies already saved are skipped). A company left
    half-read is marked ready as it is, so it never falls into the enrichment queue and spends credits."""
    _db().table(RUNS).update({"status": "queued", "steps": new_steps(), "counts": {}}).eq("status", "running").execute()
    (_db().table(store.COMPANIES).update({"status": "ready", "finished_at": store.now_iso()})
     .eq("status", "enriching").not_.is_("agent_run_id", "null").execute())


def tick() -> None:
    global _thread
    if _thread is None or not _thread.is_alive():
        queued = _db().table(RUNS).select("*").eq("status", "queued").order("created_at").limit(1).execute().data
        if queued:
            row = queued[0]
            claimed = _db().table(RUNS).update({"status": "running", "updated_at": _now().isoformat()}).eq("id", row["id"]).eq("status", "queued").execute().data
            if claimed:
                _thread = threading.Thread(target=_search, args=(claimed[0],), name="agent-search", daemon=True)
                _thread.start()
    due = (_db().table(RUNS).select("*").in_("status", ["sending", "waiting"]).lte("next_send_at", _now().isoformat())
           .order("next_send_at").limit(3).execute().data)
    for run in due:
        try:
            if run["status"] == "waiting":
                _save(run["id"], {"status": "sending"})
            _send_one(run)
        except Exception as e:
            log.warning("agent run %s couldn't send: %s: %s", run["id"], type(e).__name__, e)
            _save(run["id"], {"next_send_at": (_now() + timedelta(minutes=2)).isoformat()})
