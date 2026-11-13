"""ReachAgent's automatic runs. Both modes find companies the same way and send the same way; only the middle differs.

Without enrichment ("fast": no API key, no paid tool)
  1 find    Google Maps (gosom) for "<company type> in <city>": name, website, phone, address, rating
  2 save    new companies only (a website already in ReachAgent is skipped)
  3 read    each company's website: emails, phone, social links, the people it names
  4 emails  one address per company: a named person's email on the site, else a named person's email guessed and
            confirmed by the company's own mail server, else the company address (info@, office@)
  5 send    the agent message from Settings

With enrichment ("smart": the user's own tool keys)
  1 find, 2 save as above, then
  3 enrich  every company goes through the 8-step pipeline (engine.py) run by the lead finder loop: the decision makers
            and their emails from Hunter, checked by Reoon, a personal line written by the AI; only verified people are
            picked
  4 send    "Your email" from Settings, with each person's personal line

Bulk send (the user's own list and message)
  1 check   every address: written right, its domain takes email, not emailed before, and Reoon's check when connected
  2 save    the list, one company per domain, so it shows in Results
  3 send    the user's message, {{greeting}} being "Hi John," when the list gives a name, else "Hi there,"

Finding runs on its own thread, one run at a time on the server. Sending is one email per tick, with a gap, within the
daily limit and test mode, so a run can be stopped at any moment and carries on after a restart or the next day.
"""
import logging
import random
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from app.db.supabase import get_supabase
from app.services.lead_finder import gmaps, keys, mailbox, mailcheck, store, website
from app.services.lead_finder.constants import ALWAYS_SKIP, STEP_IDS, is_us, type_label
from app.services.lead_finder.providers import ProviderError, ReoonVerifier

log = logging.getLogger("uvicorn.error")

RUNS = "prospect_agent_runs"
STEPS = {
    "fast": [("find", "Find companies on Google Maps"), ("save", "Save new companies"), ("read", "Read their websites"),
             ("emails", "Find an email for each"), ("send", "Send the message")],
    "smart": [("find", "Find companies on Google Maps"), ("save", "Save new companies"),
              ("enrich", "Find the owners and their emails (Hunter), check them (Reoon), write personal lines (AI)"),
              ("send", "Send your email to the verified people")],
    "bulk": [("check", "Check every email on your list"), ("save", "Save the list"), ("send", "Send your message")],
}
_EMAIL = re.compile(r"^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.[a-z]{2,24}$")
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

def new_steps(mode: str = "fast") -> list[dict]:
    return [{"id": i, "label": label, "state": "pending", "note": ""} for i, label in STEPS.get(mode, STEPS["fast"])]


def start(user_id: str, filters: dict) -> dict:
    mode = filters.get("mode", "fast")
    row = {"user_id": user_id, "mode": mode, "filters": filters, "status": "queued",
           "steps": new_steps(mode), "counts": {}, "processed": []}
    return _db().table(RUNS).insert(row).execute().data[0]


def list_runs(user_id: str, limit: int = 10) -> list[dict]:
    return (_db().table(RUNS).select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute().data)


def get(user_id: str, run_id: str) -> dict | None:
    rows = _db().table(RUNS).select("*").eq("user_id", user_id).eq("id", run_id).limit(1).execute().data
    if not rows:
        return None
    companies = (_db().table(store.COMPANIES).select(store.WITH_CONTACTS).eq("user_id", user_id)
                 .eq("agent_run_id", run_id).order("created_at").execute().data)
    return {**rows[0], "companies": [store._with_contacts(c) for c in companies]}


def stop(user_id: str, run_id: str) -> dict | None:
    rows = (_db().table(RUNS).update({"status": "stopped", "finished_at": _now().isoformat(), "updated_at": _now().isoformat()})
            .eq("user_id", user_id).eq("id", run_id).in_("status", ["queued", "running", "enriching", "sending", "waiting"]).execute().data)
    if rows and rows[0].get("mode") == "smart":
        # Companies still waiting for enrichment don't spend credits after a stop
        (_db().table(store.COMPANIES).update({"status": "failed", "error": "The agent run was stopped before this company was looked into.",
                                              "finished_at": _now().isoformat()})
         .eq("agent_run_id", run_id).eq("status", "queued").execute())
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

    def log(self, text: str, **kv):
        """One line in the live activity feed (the latest 40 are kept)"""
        self.counts["log"] = (self.counts.get("log") or [])[-39:] + [{"t": _now().isoformat(), "text": text}]
        self.count(**kv)

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


def _bulk(row: dict) -> None:
    """Bulk send: check the user's list, save it, then the normal sending takes over"""
    run, f, uid = _Run(row), row["filters"], row["user_id"]
    try:
        recips = f.get("recipients") or []
        run.log(f"Checking {len(recips)} addresses", total=len(recips))
        run.step("check", "running", f"0 of {len(recips)}")
        settings = store.get_settings(uid)
        reoon = (keys.load(uid).get("reoon") or {}).get("key") if settings.get("tools", {}).get("verifier", True) else None
        seen, good, removed = set(), [], 0
        with httpx.Client() as client:
            verifier = ReoonVerifier(reoon, client) if reoon else None
            for i, r in enumerate(recips, 1):
                if run.stopped():
                    return
                e = r["email"].lower()
                reason, status = None, "unknown"
                if e in seen:
                    reason = "listed twice"
                elif not _EMAIL.match(e):
                    reason = "not a valid email address"
                elif not mailcheck.receives_email(e.split("@")[1]):
                    reason = "its domain doesn't receive email"
                elif _emailed_before(uid, e):
                    reason = "already emailed before"
                elif verifier:
                    try:
                        status = verifier.verify(e)
                        if status == "invalid":
                            reason = "Reoon says this mailbox doesn't exist"
                    except ProviderError as err:
                        run.log(f"Reoon stopped checking: {err}")
                        if err.fatal:
                            verifier = None
                seen.add(e)
                if reason:
                    removed += 1
                    run.log(f"Removed {e}: {reason}")
                else:
                    good.append({**r, "email": e, "status": status})
                run.count(checked=i, emails=len(good), removed=removed)
                if i % 10 == 0 or i == len(recips):
                    run.step("check", "running", f"{i} of {len(recips)}: {len(good)} good, {removed} removed")
        run.step("check", "done", f"{len(good)} ready to send, {removed} removed")
        if not good:
            run.step("save", "done", "Nothing to save")
            run.step("send", "done", "Nothing to send")
            _save(run.id, {"status": "done", "finished_at": _now().isoformat()})
            return

        # One company per domain (each free-mail address is its own), so the list shows in Results like any other
        groups: dict[str, list[dict]] = {}
        for r in good:
            domain = r["email"].split("@")[1]
            groups.setdefault(r["email"] if domain in mailcheck.FREE_MAIL else domain, []).append(r)
        rows = []
        for key, people in groups.items():
            domain = key if "@" not in key else None
            name = next((p["company"] for p in people if p.get("company")), None) or (domain or people[0].get("name") or key)
            r = store.company_row(uid, {"name": name, "website": domain or "", "country": "", "city": "", "company_type": None}, "search")
            r.update(status="ready", agent_run_id=run.id, finished_at=store.now_iso(),
                     steps=[{"id": s, "state": "done", "note": "Added from your bulk list"} for s in STEP_IDS])
            rows.append(r)
        saved = _db().table(store.COMPANIES).insert(rows).execute().data
        for company, people in zip(saved, groups.values()):
            store.replace_contacts(company, [{
                "name": p.get("name") or p["email"], "title": None, "email": p["email"],
                "email_status": "valid" if p["status"] == "valid" else "catch_all" if p["status"] == "catch_all" else "unknown",
                "email_source": "Your list", "linkedin": None, "personal_line": "", "selected": True,
                "verified_at": store.now_iso() if p["status"] != "unknown" else None,
            } for p in people])
        run.log(f"Saved {len(good)} people at {len(saved)} companies", new=len(saved))
        run.step("save", "done", f"{len(good)} people saved")
        run.step("send", "running", "Starting")
        _save(run.id, {"status": "sending", "next_send_at": _now().isoformat()})
    except Exception as e:
        log.warning("agent bulk run %s failed: %s: %s", run.id, type(e).__name__, e)
        _save(run.id, {"status": "failed", "error": "Something went wrong while checking your list. Try again.", "finished_at": _now().isoformat()})


def _search(row: dict) -> None:
    if row.get("mode") == "bulk":
        return _bulk(row)
    run, f, uid = _Run(row), row["filters"], row["user_id"]
    try:
        settings = store.get_settings(uid)
        run.step("find", "running", f"Searching “{_query(f)}”")
        run.log(f"Searching Google Maps for “{_query(f)}”")
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
        run.log(f"Google Maps gave {len(places)} places: {len(picked)} are new and have a website", found=len(places), new=len(picked))
        run.step("find", "done", f"{len(places)} on Google Maps, {len(picked)} new with a website")
        if not picked:
            for s in run.steps[1:]:
                run.step(s["id"], "done", "Nothing new to do")
            _save(run.id, {"status": "done", "finished_at": _now().isoformat()})
            return

        smart = row.get("mode") == "smart"
        run.step("save", "running")
        rows = []
        for p in picked:
            r = store.company_row(uid, {"name": p["name"], "website": p["domain"], "country": f["country"], "state": f.get("state"),
                                        "city": f["city"], "company_type": f["company_type"]}, "search")
            # With enrichment the company waits in the normal queue ("queued") and the lead finder loop runs its 8 steps
            r.update(status="queued" if smart else "enriching", phone=p["phone"], address=p["address"], agent_run_id=run.id,
                     rating=round(p["rating"], 1) if p["rating"] else None, reviews=p["reviews"],
                     sources=[{"label": "Google Maps", "url": p["maps_url"]}] if p["maps_url"] else [])
            rows.append(r)
        saved = _db().table(store.COMPANIES).insert(rows).execute().data
        run.step("save", "done", f"{len(saved)} companies saved")

        if smart:
            run.log(f"{len(saved)} companies handed to enrichment: Hunter finds the owners, Reoon checks emails, the AI writes lines")
            run.step("enrich", "running", f"0 of {len(saved)} done")
            _save(run.id, {"status": "enriching"})
            return

        run.step("read", "running", f"0 of {len(saved)}")
        run.step("emails", "running")
        with_email = 0
        with httpx.Client(follow_redirects=False) as client:
            for i, c in enumerate(saved, 1):
                if run.stopped():
                    return
                run.log(f"Reading {c['domain']} ({c['name']})", current=c["domain"])
                site = website.read_site(client, c["domain"], c["name"], c["city"])
                if site.pages:
                    run.log(f"{c['domain']}: read {', '.join(site.pages)}; {len(site.emails)} email(s), {len(site.people)} named person(s)")
                else:
                    run.log(f"{c['domain']}: couldn't read the website ({site.error})")
                contact, how = _pick_contact(c, c["domain"], site)
                run.log(f"{c['domain']}: {contact['email'] + ' (' + how + ')' if contact else 'no email: ' + how}")
                if contact:
                    with_email += 1
                    store.replace_contacts(c, [{**contact, "linkedin": None, "personal_line": "", "selected": True}])
                store.update_company(c["id"], {
                    "status": "ready", "finished_at": store.now_iso(), "error": None,
                    "steps": _company_steps(c["domain"], site, contact, how),
                    "phone": c.get("phone") or (site.phones[0] if site.phones else None),
                    "socials": site.socials, "founded_year": site.founded_year, "technologies": site.technologies,
                })
                run.count(read=i, emails=with_email, current=None)
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


def render_bulk(f: dict, company: dict, contact: dict) -> tuple[str, str]:
    """The bulk message: "Hi John," when the list named the person, else "Hi there," """
    named = contact.get("name") and "@" not in contact["name"]
    first = contact["name"].split()[0] if named else ""
    # The company name is real only when the list gave one; otherwise it was made from the domain, email or name
    has_company = company.get("name") not in (company.get("domain"), contact.get("email"), contact.get("name"))

    def fill(text: str) -> str:
        for key, value in (("greeting", f"Hi {first}," if first else "Hi there,"), ("first_name", first or "there"),
                           ("company", company["name"] if has_company else "your company")):
            text = re.sub(r"\{\{\s*" + key + r"\s*\}\}", lambda _, v=value: v, text)
        return text

    return fill(f.get("subject") or ""), fill(f.get("message") or "").strip()


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


def _add_log(counts: dict, text: str) -> None:
    counts["log"] = (counts.get("log") or [])[-39:] + [{"t": _now().isoformat(), "text": text}]


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

    # Last look before sending: a "person" whose name is a heading ("EMPLOYEE- STORIES") is not someone to write to
    if run.get("mode") != "bulk" and contact.get("title") != "Company email" and not website.looks_like_person(contact.get("name") or ""):
        store.update_contact(uid, contact["id"], {"send_error": "Skipped: this doesn't look like a real person's name", "selected": False})
        counts["skipped"] = counts.get("skipped", 0) + 1
        _add_log(counts, f"Skipped {contact['email']}: “{contact.get('name')}” isn't a real person")
        return _save(rid, {"processed": processed, "counts": counts})

    if not test and _emailed_before(uid, contact["email"]):
        store.update_contact(uid, contact["id"], {"send_error": "Already emailed before"})
        counts["skipped"] = counts.get("skipped", 0) + 1
        _add_log(counts, f"Skipped {contact['email']}: already emailed before")
        return _save(rid, {"processed": processed, "counts": counts})

    # With enrichment it is "Your email" with the person's personal line; bulk, the message written for this run;
    # without enrichment, the agent message
    mode = run.get("mode")
    subject, body = (mailbox.render(settings, company, contact) if mode == "smart"
                     else render_bulk(run["filters"], company, contact) if mode == "bulk"
                     else render(settings, company, contact))
    if test:
        note = f"TEST MODE: with test mode off, this email goes to {contact['name']} <{contact['email']}>.\n\n"
        msg = mailbox.build(mb, mb["email"], mb.get("from_name") or "", f"[Test] {subject}", note + body)
    else:
        to_name = contact["name"] if contact.get("title") != "Company email" and "@" not in (contact.get("name") or "") else ""
        msg = mailbox.build(mb, contact["email"], to_name, subject, body)
    try:
        error = mailbox.send(mb, [msg], copy_to_sent=not test)[0]
    except mailbox.MailboxError as e:
        return _finish(run, "failed", "Your mailbox couldn't send", str(e))

    if error:
        store.update_contact(uid, contact["id"], {"send_error": error})
        counts["failed"] = counts.get("failed", 0) + 1
        _add_log(counts, f"Not sent to {contact['email']}: {error}")
    else:
        counts["sent"] = counts.get("sent", 0) + 1
        _add_log(counts, f"Test email for {company['name']} ({contact['email']}) sent to you" if test
                 else f"Sent to {contact['email']} ({company['name']})")
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
    half-read by a run without enrichment is marked ready as it is, so it never falls into the enrichment queue and
    spends credits. (With enrichment, the lead finder loop recovers its own companies.)"""
    for r in _db().table(RUNS).select("id,mode").eq("status", "running").execute().data:
        _save(r["id"], {"status": "queued", "steps": new_steps(r.get("mode") or "fast"), "counts": {}})
    fast = [r["id"] for r in _db().table(RUNS).select("id").eq("mode", "fast").order("created_at", desc=True).limit(50).execute().data]
    if fast:
        (_db().table(store.COMPANIES).update({"status": "ready", "finished_at": store.now_iso()})
         .eq("status", "enriching").in_("agent_run_id", fast).execute())


def _follow_enrichment(run: dict) -> None:
    """With enrichment: watch the lead finder loop work through this run's companies, then start sending"""
    cos = (_db().table(store.COMPANIES).select(f"name,domain,status, {store.CONTACTS}(email,email_status,selected)")
           .eq("agent_run_id", run["id"]).execute().data)
    total = len(cos)
    done = [c for c in cos if c["status"] in ("ready", "approved", "failed")]
    people = [p for c in done for p in (c.get(store.CONTACTS) or [])]
    picked = [p for p in people if p.get("selected") and p.get("email")]
    counts = dict(run.get("counts") or {})
    steps = run.get("steps") or new_steps("smart")
    if len(done) != counts.get("read"):
        for c in done:
            if c["domain"] not in (counts.get("done_domains") or []):
                found = [p for p in (c.get(store.CONTACTS) or []) if p.get("selected") and p.get("email")]
                _add_log(counts, f"{c['domain']}: " + (f"{len(found)} verified ({', '.join(p['email'] for p in found[:3])})"
                                                       if found else "no verified email" if c["status"] != "failed" else "couldn't finish"))
                counts["done_domains"] = (counts.get("done_domains") or []) + [c["domain"]]
    working = next((c["domain"] for c in cos if c["status"] == "enriching"), None)
    counts.update(read=len(done), people=len(people), emails=len(picked), current=working)
    for s in steps:
        if s["id"] == "enrich":
            s["note"] = f"{len(done)} of {total} done, {len(picked)} verified email{'' if len(picked) == 1 else 's'} picked"
            if len(done) == total:
                s["state"] = "done"
    patch = {"counts": counts, "steps": steps}
    if len(done) == total:
        patch.update(status="sending", next_send_at=_now().isoformat())
        for s in steps:
            if s["id"] == "send":
                s["state"], s["note"] = "running", "Starting"
    _save(run["id"], patch)


def tick() -> None:
    global _thread
    for run in _db().table(RUNS).select("*").eq("status", "enriching").execute().data:
        try:
            _follow_enrichment(run)
        except Exception as e:
            log.warning("agent run %s couldn't follow enrichment: %s: %s", run["id"], type(e).__name__, e)
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
