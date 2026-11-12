"""Find companies: turns the filters picked in "Find companies" into a Hunter Discover search (free: it uses no credits).

Hunter's natural-language `query` ignores the size filter, so the request is built from structured filters instead:
where (country, US state, city), what kind (an industry and keywords from the company type and trade), how big
(headcount ranges) and who owns it. Results come back as {name, domain, emails}: Hunter doesn't say where a company is
or how big, so those come from the filters you picked.
"""
from app.services.lead_finder.constants import COMPANY_TYPES, COUNTRY_CODES, TRADES, SIZES
from app.services.lead_finder.providers import Hunter
from app.services.lead_finder.website import clean_domain

MAX_LIMIT = 100

US_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# Hunter's own industry names (checked against its API); the first is tried, and a search whose industry Hunter refuses is
# repeated on keywords alone
INDUSTRIES = {
    "gc": ["Construction"], "sub": ["Construction"], "design_build": ["Construction"], "cm": ["Construction"],
    "home_builder": ["Construction"], "remodeler": ["Construction"], "developer": ["Real Estate"],
    "architect": ["Architecture and Planning"], "engineer": ["Civil Engineering", "Mechanical or Industrial Engineering"],
    "interior": ["Interior Design"], "supplier": ["Building Materials"], "other": [],
}
TYPE_KEYWORDS = {
    "gc": ["general contractor", "general contracting"], "sub": ["subcontractor", "specialty contractor"],
    "design_build": ["design-build", "design build"], "cm": ["construction management"],
    "developer": ["real estate development", "developer"], "home_builder": ["home builder", "custom homes"],
    "remodeler": ["remodeling", "remodeler", "renovation"], "architect": ["architecture", "architectural", "architect"],
    "engineer": ["engineering"], "interior": ["interior design"], "supplier": ["building materials", "supplier"], "other": [],
}
TRADE_KEYWORDS = {
    "electrical": ["electrical contractor", "electrician"], "plumbing": ["plumbing contractor", "plumber"],
    "hvac": ["hvac", "heating and air conditioning"], "concrete": ["concrete contractor"], "framing": ["framing contractor"],
    "masonry": ["masonry"], "steel": ["steel erection", "structural steel"], "glazing": ["glazing", "glass and windows"],
    "roofing": ["roofing contractor", "roofer"], "drywall": ["drywall", "insulation contractor"],
    "painting": ["painting contractor"], "flooring": ["flooring contractor"], "tile": ["tile contractor", "stone"],
    "carpentry": ["finish carpentry", "carpentry"], "sitework": ["excavation", "demolition"],
    "fire": ["fire protection", "fire sprinkler"], "solar": ["solar installation", "solar contractor"], "landscaping": ["landscaping"],
}
# Our size ranges -> Hunter's. Hunter has no 51-100 / 101-200 split, and "1000+" is its three biggest ranges.
HUNTER_SIZES = {
    "1-10": ["1-10"], "11-50": ["11-50"], "51-100": ["51-200"], "101-200": ["51-200"], "201-500": ["201-500"],
    "501-1000": ["501-1000"], "1000+": ["1001-5000", "5001-10000", "10001+"],
}
OWNERSHIP = {"Private": "privately held", "Partnership": "partnership", "Public": "public company"}


def _codes(values: list[str]) -> list[str]:
    out = []
    for v in values:
        code = COUNTRY_CODES.get(v.strip().lower())
        if not code:
            raise ValueError(f"“{v}” isn't a country we can search yet.")
        out.append(code.upper())
    return list(dict.fromkeys(out))


def _locations(f: dict) -> list[dict]:
    countries = _codes(f.get("countries") or [])
    cities = [c.strip() for c in f.get("cities") or [] if c.strip()]
    states = [US_STATE_CODES.get(s.strip().lower(), s.strip().upper()) for s in f.get("states") or [] if s.strip()]
    if not countries:
        raise ValueError("Pick a country first.")
    if cities:
        if len(countries) > 1:
            raise ValueError("Pick just one country when you search by city.")
        state = states[0] if countries == ["US"] and len(states) == 1 else None
        return [{"country": countries[0], **({"state": state} if state else {}), "city": c} for c in cities]
    if states and countries == ["US"]:
        return [{"country": "US", "state": s} for s in states]
    return [{"country": c} for c in countries]


def build_body(f: dict) -> dict:
    """The Hunter Discover request for these filters. Raises ValueError with a plain message when the filters can't make one."""
    types = [t for t in f.get("company_types") or [] if t in COMPANY_TYPES]
    trades = [t for t in f.get("trades") or [] if t in TRADES] if "sub" in types else []
    user_words = [w.strip().lower() for w in f.get("keywords") or [] if w.strip()]
    match = "all" if f.get("keyword_match") == "all" and user_words else "any"
    if not types and not user_words:
        raise ValueError("Pick a company type or type a keyword, or the search would return every kind of company.")

    type_words = [] if match == "all" else [w for t in types if not (t == "sub" and trades) for w in TYPE_KEYWORDS[t]]
    trade_words = [] if match == "all" else [w for t in trades for w in TRADE_KEYWORDS[t]]
    keywords = list(dict.fromkeys([*trade_words, *type_words, *user_words]))
    industries = list(dict.fromkeys(i for t in types for i in INDUSTRIES[t]))

    body: dict = {"headquarters_location": {"include": _locations(f)}, "limit": max(1, min(MAX_LIMIT, int(f.get("limit") or 25)))}
    if industries:
        body["industry"] = {"include": industries}
    if keywords:
        body["keywords"] = {"include": keywords, "match": match}
    sizes = [h for s in f.get("sizes") or [] if s in SIZES for h in HUNTER_SIZES[s]]
    if sizes:
        body["headcount"] = list(dict.fromkeys(sizes))
    if f.get("ownership") in OWNERSHIP:
        body["company_type"] = {"include": [OWNERSHIP[f["ownership"]]]}
    return body


def search(hunter: Hunter, f: dict, in_list: set[str]) -> dict:
    """{results: [{name, domain, emails, in_list}], total, request}. Companies you never want (typed in the filters) and,
    unless asked not to, the ones already in Lead Finder are left out."""
    body = build_body(f)
    try:
        data = hunter.discover(body)
    except Exception as e:
        # Hunter refused the industry name: look again on the keywords alone rather than giving up
        if getattr(e, "code", None) == "invalid_industry" and "industry" in body and "keywords" in body:
            body = {k: v for k, v in body.items() if k != "industry"}
            data = hunter.discover(body)
        else:
            raise
    blocked = {clean_domain(d) for d in f.get("exclude_domains") or [] if d.strip()}
    hide_known = bool(f.get("exclude_in_list", True))
    results = []
    for row in data["companies"]:
        domain = clean_domain(row["domain"])
        if not domain or domain in blocked or any(domain.endswith("." + b) for b in blocked):
            continue
        known = domain in in_list
        if known and hide_known:
            continue
        results.append({"name": row["name"], "domain": domain, "emails": row["emails"], "in_list": known})
    return {"results": results, "total": data["total"], "request": body}
