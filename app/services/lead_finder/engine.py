"""Runs the 8 steps for one company. No database in here: `save` writes progress and `find_cached` looks up a website
that was already looked into, so the whole thing can be tested with fake tools.

  1 website   the one you gave, else Hunter (by company name), else Google search; directories are never taken
  2 cache     the same website looked into in the last 30 days is reused, so credits aren't spent twice
  3 read      home page plus up to 4 team / about / contact pages
  4 extract   emails, phone, social links, "since 1998", what the site is built with, Google rating, recent news
  5 people    Hunter's people plus the website's, ranked by the job titles for this company type
  6 emails    the website's own, else Hunter's email finder, else the company's email pattern
  7 verify    Reoon (or Hunter's own check when it isn't connected)
  8 line      the personal line: written by the AI from what was found (checked against the facts), else from the
              templates; only what was found either way
Every step leaves a short note saying what it did, in plain words. A tool that fails only skips its part.
"""
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus

from app.services.lead_finder import mailcheck
from app.services.lead_finder import news as news_mod
from app.services.lead_finder import people as people_mod
from app.services.lead_finder import writer as writer_mod
from app.services.lead_finder import website
from app.services.lead_finder.constants import ALWAYS_SKIP, STEP_IDS, country_code, roles_for, type_label
from app.services.lead_finder.personalize import pick_line
from app.services.lead_finder.providers import Apollo, GooglePlacesAPI, Hunter, PDL, ProviderError, ReoonVerifier, Serper

NO_WEBSITE = "We couldn't find a website for this company. Add its website and try again."


@dataclass
class Tools:
    """What this run may use; None = not connected or switched off in Settings"""
    hunter: Hunter | None = None
    serper: Serper | None = None          # Google search, for the website
    places: Serper | None = None          # Google Maps (via Serper), for rating/phone/address
    verifier: ReoonVerifier | None = None
    writer: writer_mod.Writer | None = None        # Gemini: writes the personal line
    read_site: Callable[[str, str, str], website.SiteData] | None = None
    apollo: Apollo | None = None          # Fallback email finder when Hunter has no match
    gplaces: GooglePlacesAPI | None = None # Direct Google Places API fallback for rating/phone/address
    openai_writer: writer_mod.Writer | None = None # OpenAI fallback when Gemini is unavailable
    pdl: PDL | None = None                # Enriches contacts with professional background


@dataclass
class Result:
    patch: dict
    contacts: list[dict] = field(default_factory=list)


def pending_steps() -> list[dict]:
    return [{"id": s, "state": "pending"} for s in STEP_IDS]


class _Steps:
    """The 8 steps' states; each finished step is saved together with what it found, and the next one starts"""

    def __init__(self, save: Callable[[dict], None]):
        self.items = pending_steps()
        self.save = save

    def _i(self, sid: str) -> int:
        return STEP_IDS.index(sid)

    def copy(self) -> list[dict]:
        return [dict(s) for s in self.items]

    def start(self, sid: str):
        self.items[self._i(sid)]["state"] = "running"
        self.save({"steps": self.copy()})

    def done(self, sid: str, note: str, **found):
        i = self._i(sid)
        self.items[i].update(state="done", note=note)
        if i + 1 < len(self.items):
            self.items[i + 1]["state"] = "running"
        self.save({"steps": self.copy(), **found})

    def fail(self, sid: str, note: str):
        self.items[self._i(sid)].update(state="failed", note=note)

    def skip_rest(self, after: str, note: str):
        for s in self.items[self._i(after) + 1:]:
            s.update(state="done", note=note)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _best_place(places: list[dict], company_name: str, domain: str, phone: str | None) -> dict | None:
    """The Google listing that really is this company. A big company has many branches under one name, so the name alone can
    land on the wrong office: prefer listings with the company's own website, and among those the one with the phone
    number the website shows (its main office)."""
    named = [p for p in places if _place_matches(p["title"], company_name)]

    def same_site(p: dict) -> bool:
        site = website.clean_domain(p.get("website") or "")
        return bool(site and domain and (site == domain or site.endswith("." + domain) or domain.endswith("." + site)))

    pool = [p for p in named if same_site(p)] or named
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) >= 7:
        for p in pool:
            if re.sub(r"\D", "", p.get("phone") or "")[-10:] == digits:
                return p
    return pool[0] if pool else None


def _place_matches(title: str, company_name: str) -> bool:
    tokens = set(website.name_tokens(company_name))
    words = set(website.name_tokens(title))
    if not tokens or not words:
        return False
    # "Turner" is Turner Construction's Maps name: every word of the shorter name is in the other
    return words <= tokens or tokens <= words or len(words & tokens) >= min(2, len(tokens))


def enrich(company: dict, settings: dict, tools: Tools, save: Callable[[dict], None],
           find_cached: Callable[[str], dict | None], now: Callable[[], str]) -> Result:
    steps = _Steps(save)
    name, city, state, country = company["name"], company.get("city") or "", company.get("state") or "", company.get("country") or ""
    cc = country_code(country)
    skip = list(settings.get("skip_sites") or []) + list(ALWAYS_SKIP)
    found: dict = {}
    sources: list[dict] = []

    def finish(contacts: list[dict]) -> Result:
        return Result({**found, "status": "ready", "error": None, "sources": sources, "steps": steps.copy(), "finished_at": now()}, contacts)

    # ---------- 1. Website ----------
    steps.start("website")
    domain = website.clean_domain(company.get("domain") or "") or None
    hunter_result = None
    if domain:
        if not website.valid_domain(domain):
            steps.fail("website", f"“{domain}” isn't a website address")
            return Result({"status": "failed", "error": "That website address isn't valid. Fix it and try again.", "steps": steps.copy(), "finished_at": now()})
        note = f"Using the website you gave: {domain}"
    else:
        tried, problems = [], []
        if tools.hunter:
            tried.append("Hunter")
            try:
                hs = tools.hunter.domain_search(company=name, limit=10)
                d = hs["domain"]
                # Only the website's own address proves it is this company: Hunter may echo the searched name for another site
                if d and website.matches_name(d, name) and not website.skipped(d, skip):
                    domain, hunter_result = d, hs
                    note = f"Found {d} (Hunter)"
            except ProviderError as e:
                problems.append(str(e))
        if not domain and tools.serper:
            tried.append("Google search")
            query = " ".join(x for x in (f'"{name}"', city, state, country) if x)
            try:
                hit = website.pick_website(tools.serper.search(query, cc), name, skip)
                if hit:
                    domain = hit[0]
                    note = f"Found {domain} (Google search)"
                    sources.append({"label": "Google search result", "url": hit[1]})
            except ProviderError as e:
                problems.append(str(e))
        if not domain:
            if not tried:
                error, note = "No search tool is connected yet, so add the company's website and try again.", "No search tool is connected"
            elif problems:
                error, note = f"{problems[0]} Add the company's website and try again.", problems[0]
            else:
                error, note = NO_WEBSITE, f"{' and '.join(tried)} found no website that matches the name and city"
            steps.fail("website", note)
            return Result({"status": "failed", "error": error, "steps": steps.copy(), "finished_at": now()})
    found["domain"] = domain
    steps.done("website", note, domain=domain)

    # ---------- 2. Already looked into? ----------
    cached = find_cached(domain)
    if cached:
        when = str(cached.get("finished_at") or "")[:10]
        for key in ("phone", "address", "rating", "reviews", "socials"):
            if cached.get(key) not in (None, [], ""):
                found[key] = cached[key]
        found["founded_year"] = company.get("founded_year") or cached.get("founded_year")
        found["technologies"] = list(dict.fromkeys([*(company.get("technologies") or []), *(cached.get("technologies") or [])]))
        sources.extend(cached.get("sources") or [])
        cached_line = next((c.get("personal_line") for c in cached.get("contacts") or [] if c.get("personal_line")), "")
        picked = (cached_line, 0) if cached_line else pick_line({**company, **found}, settings.get("templates") or [])
        contacts = []
        for c in cached.get("contacts") or []:
            status = c.get("email_status") or "unknown"
            contacts.append({
                "name": c["name"], "title": c.get("title"), "email": c.get("email"), "email_status": status,
                "email_source": c.get("email_source"), "linkedin": c.get("linkedin"), "verified_at": c.get("verified_at"),
                "personal_line": picked[0] if picked and status != "invalid" else "", "selected": status == "valid",
            })
        steps.done("cache", f"Already looked into on {when}: reused, no credits spent")
        steps.skip_rest("cache", f"Reused from {when}")
        return finish(contacts)
    steps.done("cache", "New company, nothing saved before")

    # ---------- 3. Read the website ----------
    site = tools.read_site(domain, name, city) if tools.read_site else website.SiteData(error="Reading websites is switched off")
    if site.pages:
        note = f"Read {_plural(len(site.pages), 'page')}: {', '.join(site.pages)}"
        sources.extend({"label": f"Website: {n.title()} page" if n != "home" else "Website: Home page", "url": u}
                       for n, u in zip(site.pages, site.page_urls))
        # What the website says about the company, shown on the review page (and given to the AI writer)
        page_url = dict(zip(site.pages, site.page_urls))
        sources.extend({"label": f"Website: {e['page'].title()} page", "url": page_url.get(e["page"], site.page_urls[0]), "text": e["text"]}
                       for e in site.excerpts)
    else:
        note = f"Couldn't read the website ({site.error}). The other tools still run"
    steps.done("read", note)

    # ---------- 4. Pick out details (and the Google rating) ----------
    found.update(
        phone=site.phones[0] if site.phones else None,
        socials=site.socials,
        founded_year=company.get("founded_year") or site.founded_year,
        technologies=list(dict.fromkeys([*(company.get("technologies") or []), *site.technologies])),
        address=None, rating=None, reviews=None,
    )
    rating_note = ""
    if tools.places:
        try:
            listings = tools.places.places(" ".join(dict.fromkeys(x for x in (name, city, state) if x)), cc)
            p = _best_place(listings, name, domain, found["phone"])
            if p:
                found["address"] = p.get("address")
                found["phone"] = found["phone"] or p.get("phone")
                if p.get("rating"):
                    found["rating"] = round(float(p["rating"]), 1)
                    found["reviews"] = int(p.get("reviews") or 0) or None
                    rating_note = f", Google rating {found['rating']}"
                    sources.append({"label": "Google Maps", "url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name} {city}')}"})
        except ProviderError as e:
            rating_note = f". No Google rating: {e}"
    elif tools.gplaces:
        try:
            listings = tools.gplaces.search(" ".join(dict.fromkeys(x for x in (name, city, state) if x)))
            p = _best_place(listings, name, domain, found["phone"])
            if p:
                found["address"] = p.get("address")
                found["phone"] = found["phone"] or p.get("phone")
                if p.get("rating"):
                    found["rating"] = round(float(p["rating"]), 1)
                    found["reviews"] = int(p.get("reviews") or 0) or None
                    rating_note = f", Google rating {found['rating']}"
                    sources.append({"label": "Google Maps", "url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name} {city}')}"})
        except ProviderError as e:
            rating_note = f". No Google rating: {e}"
    news, news_note = [], ""
    if tools.serper:
        try:
            news = news_mod.pick(tools.serper.news(f'"{name}" {city}'.strip(), cc), name)
        except ProviderError as e:
            news_note = f". No news: {e}"
    sources.extend({"label": "News", "url": n["url"], "text": n["text"], "when": n.get("when")} for n in news)
    since = f", “since {found['founded_year']}”" if found["founded_year"] else ""
    news_count = f", {_plural(len(news), 'news item')}" if news else ""
    note = (f"{_plural(len(site.emails), 'email')}, {_plural(1 if found['phone'] else 0, 'phone')}, "
            f"{_plural(len(found['socials']), 'social link')}{since}{rating_note}{news_count}{news_note}")
    steps.done("extract", note, **{k: found[k] for k in ("phone", "socials", "founded_year", "technologies", "address", "rating", "reviews")})

    # ---------- 5. Decision makers ----------
    roles = company.get("contact_roles") or roles_for(settings, company.get("company_type"))
    kind = type_label(company.get("company_type")).lower()
    hunter_people, pattern, accept_all, people_problem = [], None, False, ""
    if tools.hunter:
        try:
            hs = hunter_result if hunter_result and hunter_result["domain"] == domain else tools.hunter.domain_search(domain=domain, limit=10)
            hunter_people, pattern, accept_all = hs["people"], hs["pattern"], hs["accept_all"]
            sources.append({"label": "Hunter: domain search", "url": f"https://hunter.io/search/{domain}"})
        except ProviderError as e:
            people_problem = str(e)
    chosen = people_mod.rank(hunter_people, site.people, roles)
    if chosen and tools.pdl:
        for p in chosen:
            try:
                pdl_data = tools.pdl.enrich(p["name"], name)
                if pdl_data:
                    p["title"] = p.get("title") or pdl_data.get("title")
                    p["linkedin"] = p.get("linkedin") or pdl_data.get("linkedin")
            except Exception:
                pass
    # Nobody named: the company's own address on its website (info@, office@) is still worth checking and emailing
    company_email = None
    if not chosen:
        own = [e for e in site.emails if website._same_site(e.split("@")[1], domain)]
        company_email = next((e for e in own if mailcheck.is_role(e)), own[0] if own else None)
        if company_email:
            chosen = [{"name": name, "title": "Company email", "email": company_email, "email_source": "Website",
                       "linkedin": None, "status": "unknown"}]
    if not chosen:
        note = "No one matching your roles was found"
        if site.emails:
            note += f". The website lists {site.emails[0]}"
        if people_problem:
            note += f". {people_problem}"
        steps.done("people", note)
        steps.skip_rest("people", "Nothing to do: no people found")
        return finish([])
    for_kind = f" for {_article(kind)} {kind}" if kind else ""
    note = (f"No one matching your roles was found, so the company email from the website is used: {company_email}" if company_email
            else f"{len(chosen)} {'person matches' if len(chosen) == 1 else 'people match'} your roles{for_kind}")
    steps.done("people", note + (f". {people_problem}" if people_problem else ""))

    # ---------- 6. Their emails ----------
    email_problem = ""
    hunter_on = tools.hunter is not None
    for p in chosen:
        if not p.get("email"):
            on_site = website.email_for_person(site.emails, p["name"], domain)
            if on_site:
                p.update(email=on_site, email_source="Website")
        if not p.get("email") and hunter_on:
            first, last = people_mod.split_name(p["name"])
            try:
                hit = tools.hunter.email_finder(domain, first, last)
                if hit:
                    p.update(email=hit["email"], email_source="Hunter", status=hit["status"], linkedin=p.get("linkedin") or hit.get("linkedin"))
            except ProviderError as e:
                email_problem = str(e)
                hunter_on = not e.fatal
        if not p.get("email") and tools.apollo:
            first, last = people_mod.split_name(p["name"])
            hit = tools.apollo.find_email(first, last, domain)
            if hit:
                p.update(email=hit["email"], email_source="Apollo",
                         linkedin=p.get("linkedin") or hit.get("linkedin"),
                         title=p.get("title") or hit.get("title") or p.get("title"))
        if not p.get("email"):
            guess = people_mod.guess_email(pattern, p["name"], domain)
            if guess:
                p.update(email=guess, email_source="Guessed from the company's email pattern", status="unknown")
    with_email = [p for p in chosen if p.get("email")]
    steps.done("emails", f"{len(with_email)} of {_plural(len(chosen), 'email')} found" + (f". {email_problem}" if email_problem else ""))

    # ---------- 7. Verify ----------
    verifier, verify_problem = tools.verifier, ""
    for p in chosen:
        if not p.get("email"):
            p["email_status"] = "none"
            continue
        status = p.get("status") or "unknown"
        if verifier:
            try:
                status = verifier.verify(p["email"])
                p["verified_at"] = now()
            except ProviderError as e:
                verify_problem = str(e)
                if e.fatal:
                    verifier = None
        if status == "unknown" and accept_all:
            status = "catch_all"  # Hunter says the company accepts mail to any address
        p["email_status"] = status
    counts = {s: sum(1 for p in chosen if p["email_status"] == s) for s in ("valid", "catch_all", "invalid", "unknown")}
    parts = [f"{counts['valid']} verified" if counts["valid"] else "", f"{counts['catch_all']} risky" if counts["catch_all"] else "",
             f"{counts['invalid']} invalid" if counts["invalid"] else "", f"{counts['unknown']} not confirmed" if counts["unknown"] else ""]
    note = ", ".join(x for x in parts if x) or "Nothing to verify"
    if not tools.verifier and with_email:
        note += ". Reoon isn't connected, so these are Hunter's own checks"
    steps.done("verify", note + (f". {verify_problem}" if verify_problem else ""))

    # ---------- 8. Personal line ----------
    line, how, ai_problem = '', '', ''
    if tools.writer:
        written = tools.writer.write(writer_mod.build_facts(company, found, site.excerpts, news), settings.get('message') or '')
        if written.line:
            line, how = written.line, 'Written by the AI from: “' + written.evidence + '”'
        else:
            ai_problem = 'The AI wrote nothing: ' + written.reason + '. '
    if not line and tools.openai_writer:
        written = tools.openai_writer.write(writer_mod.build_facts(company, found, site.excerpts, news), settings.get('message') or '')
        if written.line:
            line, how = written.line, 'Written by OpenAI from: “' + written.evidence + '”'
        elif not ai_problem:
            ai_problem = 'OpenAI wrote nothing: ' + written.reason + '. '
    if not line:
        picked = pick_line({**company, **found}, settings.get('templates') or [])
        if picked:
            line, how = picked[0], ai_problem + 'Used template ' + str(picked[1])
        else:
            how = ai_problem + 'No template fit, so the message starts without a personal line'
    steps.done("line", how)

    contacts = [{
        "name": p["name"], "title": p.get("title") or None, "email": p.get("email"), "email_status": p["email_status"],
        "email_source": p.get("email_source") or None, "linkedin": p.get("linkedin"), "verified_at": p.get("verified_at"),
        "personal_line": line if p["email_status"] != "invalid" else "", "selected": p["email_status"] == "valid",
    } for p in chosen]
    return finish(contacts)
