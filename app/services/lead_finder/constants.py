"""The lists Lead Finder uses, kept the same as frontend/src/lib/leadFinder.ts so the form, the API and the engine agree."""

STEP_IDS = ("website", "cache", "read", "extract", "people", "emails", "verify", "line")
STATUSES = ("queued", "enriching", "ready", "approved", "failed")
SOURCES = ("manual", "search", "csv")
SIZES = ("1-10", "11-50", "51-100", "101-200", "201-500", "501-1000", "1000+")
PRIORITIES = ("high", "medium", "low")
EMAIL_STATUSES = ("valid", "catch_all", "invalid", "unknown", "none")

# id -> label, US industry codes (NAICS), job titles worth emailing (best first)
COMPANY_TYPES: dict[str, dict] = {
    "gc": {"label": "General contractor", "naics": ["236"], "roles": ["Owner", "President", "Estimator", "Preconstruction Manager", "Project Manager"]},
    "sub": {"label": "Subcontractor", "naics": ["238"], "roles": ["Owner", "Estimator", "Operations Manager"]},
    "design_build": {"label": "Design-build firm", "naics": ["236"], "roles": ["Owner", "President", "Preconstruction Manager", "Design Director"]},
    "cm": {"label": "Construction manager", "naics": ["236"], "roles": ["President", "Preconstruction Manager", "Project Executive"]},
    "developer": {"label": "Developer", "naics": [], "roles": ["Owner", "Development Manager", "Director of Development"]},
    "home_builder": {"label": "Home builder", "naics": ["236115", "236117"], "roles": ["Owner", "Sales Manager", "Construction Manager"]},
    "remodeler": {"label": "Remodeler", "naics": ["236118"], "roles": ["Owner", "Sales Manager", "Project Manager"]},
    "architect": {"label": "Architect", "naics": ["541310"], "roles": ["Principal", "Partner", "Project Architect"]},
    "engineer": {"label": "Engineer", "naics": ["541330"], "roles": ["Principal", "Partner", "Project Engineer"]},
    "interior": {"label": "Interior designer", "naics": ["541410"], "roles": ["Principal", "Founder", "Senior Designer"]},
    "supplier": {"label": "Supplier / manufacturer", "naics": [], "roles": ["Owner", "Sales Manager", "Business Development"]},
    "other": {"label": "Other", "naics": [], "roles": []},
}

# Subcontractor trades: id -> (label, NAICS)
TRADES: dict[str, tuple[str, str]] = {
    "electrical": ("Electrical", "238210"),
    "plumbing": ("Plumbing", "238220"),
    "hvac": ("HVAC", "238220"),
    "concrete": ("Concrete", "238110"),
    "framing": ("Framing", "238130"),
    "masonry": ("Masonry", "238140"),
    "steel": ("Steel / metal", "238120"),
    "glazing": ("Glass & windows", "238150"),
    "roofing": ("Roofing", "238160"),
    "drywall": ("Drywall & insulation", "238310"),
    "painting": ("Painting", "238320"),
    "flooring": ("Flooring", "238330"),
    "tile": ("Tile & stone", "238340"),
    "carpentry": ("Finish carpentry", "238350"),
    "sitework": ("Excavation & demolition", "238910"),
    "fire": ("Fire protection", "238220"),
    "solar": ("Solar", "238210"),
    "landscaping": ("Landscaping", "561730"),
}

US_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
# Two-letter codes for Serper's `gl` (which country's Google to search)
COUNTRY_CODES = {
    "united states": "us", "united states of america": "us", "usa": "us", "us": "us",
    "canada": "ca", "united kingdom": "gb", "uk": "gb", "pakistan": "pk",
    "united arab emirates": "ae", "uae": "ae", "saudi arabia": "sa",
    "india": "in", "australia": "au", "germany": "de", "france": "fr", "ireland": "ie", "qatar": "qa", "new zealand": "nz",
}

DEFAULT_ROLES: dict[str, list[str]] = {
    "default": ["Owner", "Founder", "CEO", "Managing Director", "Principal"],
    **{tid: t["roles"] for tid, t in COMPANY_TYPES.items() if t["roles"]},
}

# Never a company's own website, even when Settings lists nothing: directories, maps, review and social sites
ALWAYS_SKIP = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "yelp.com", "yellowpages.com", "bbb.org", "mapquest.com", "angi.com", "angieslist.com", "houzz.com", "homeadvisor.com",
    "thumbtack.com", "buildzoom.com", "manta.com", "dnb.com", "zoominfo.com", "crunchbase.com", "bloomberg.com",
    "glassdoor.com", "indeed.com", "wikipedia.org", "google.com", "apple.com", "bing.com", "zameen.com", "olx.com.pk",
)

DEFAULT_TEMPLATES = [
    {"id": "t1", "text": "Saw {{company}}'s {{rating}}★ rating from {{reviews}} reviews on Google. That's a lot of happy clients.", "enabled": True},
    {"id": "t2", "text": "Noticed {{company}} has been working in {{city}} since {{founded_year}}.", "enabled": True},
    {"id": "t3", "text": "I've been talking to a few {{industry}} firms in {{city}} lately.", "enabled": True},
    # Your own opening line, used when nothing more personal was found
    {"id": "g1", "text": "I have noticed that you do a lot of projects.", "enabled": True},
]

DEFAULT_SETTINGS = {
    "roles": DEFAULT_ROLES,
    "skip_sites": ["linkedin.com", "facebook.com", "instagram.com", "yelp.com", "yellowpages.com", "houzz.com", "angi.com", "zameen.com"],
    "templates": DEFAULT_TEMPLATES,
    # The team's own generic message (the one their campaigns send). {{personal_line}} replaces its opening sentence with
    # one written for each company from what was found about it.
    "subject": "Drafting support for {{company}}",
    "message": (
        "Hi {{first_name}},\n\n{{personal_line}}\n\nIf your team could use extra drafting capacity, we can assist with revisions, "
        "updated plans, and city comment responses, helping accelerate approvals while reducing the burden on your internal staff. "
        "We specialize in permit-ready plans, drafting, and permit support with a proven track record of helping projects move "
        "smoothly from submission to approval.\n\nGary | Design Head\nCADTRI | (737) 237-2736"
    ),
    "daily_limit": 50,
    "at_once": 2,
    "tools": {"hunter": True, "serper": True, "verifier": True, "places": True, "ai": True,
              "apollo": True, "gplaces": True, "openai": True, "pdl": True},
}


# The placeholder message saved before the real one was set: a saved copy of it is replaced by the defaults
OLD_PLACEHOLDER = "[Say in one or two lines what you offer and why it helps them.]"


def is_us(country: str | None) -> bool:
    return (country or "").strip().lower() in US_NAMES


def country_code(country: str | None) -> str | None:
    return COUNTRY_CODES.get((country or "").strip().lower())


def type_label(company_type: str | None) -> str:
    return COMPANY_TYPES.get(company_type or "", {}).get("label", "")


def naics_for(company_type: str | None, trades: list[str]) -> list[str]:
    """US industry codes for a company type and its trades. A subcontractor with trades uses the trades' own codes."""
    from_trades = [TRADES[t][1] for t in trades if t in TRADES]
    from_type = [] if company_type == "sub" and from_trades else COMPANY_TYPES.get(company_type or "", {}).get("naics", [])
    return list(dict.fromkeys([*from_type, *from_trades]))


def roles_for(settings: dict, company_type: str | None) -> list[str]:
    """Who to look for at a company of this type: its own list in Settings, else the "any other company" list."""
    roles = settings.get("roles") or {}
    own = roles.get(company_type or "") if company_type else None
    return own or roles.get("default") or DEFAULT_ROLES["default"]
