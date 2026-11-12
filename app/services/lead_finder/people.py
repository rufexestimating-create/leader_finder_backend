"""Who to email at a company: everyone Hunter and the website name, ranked by the job titles in Settings (best first)."""
import re

# Hunter's seniority, when no job title matches
_SENIORITY = {"executive": 0, "senior": 1, "junior": 2}


# "Vice President" is not the President, nor "Assistant Project Manager" the Project Manager
_NOT_THE_ROLE = re.compile(r"(?:vice|assistant|asst\.?|deputy|associate)[\s-]+$")


def role_rank(title: str, roles: list[str]) -> int | None:
    """Position of the first role the title contains ("Co-Owner & President" contains "Owner"), or None.
    A role right after "Vice", "Assistant", "Deputy" or "Associate" doesn't count."""
    t = (title or "").lower()
    for i, role in enumerate(roles):
        r = role.strip().lower()
        if not r:
            continue
        for m in re.finditer(rf"(?<![a-z]){re.escape(r)}(?![a-z])", t):
            if not _NOT_THE_ROLE.search(t[: m.start()]):
                return i
    return None


def split_name(name: str) -> tuple[str, str]:
    words = name.split()
    return (words[0], words[-1]) if len(words) >= 2 else (name, "")


def rank(hunter_people: list[dict], site_people: list[dict], roles: list[str], limit: int = 3) -> list[dict]:
    """The best `limit` people. Anyone whose title matches a role comes first, in the roles' order; when nobody
    matches, Hunter's decision makers and executives are used instead. Returns
    [{name, title, email, email_source, linkedin, status}] (status = Hunter's own check, "unknown" when none)."""
    people: dict[str, dict] = {}
    for p in hunter_people:
        if p.get("type") == "generic":  # info@, sales@: not a person
            continue
        name = " ".join(f"{p.get('first_name') or ''} {p.get('last_name') or ''}".split())
        if len(name.split()) < 2:
            continue
        people[name.lower()] = {
            "name": name, "title": p.get("title") or "", "email": p.get("email"), "email_source": "Hunter" if p.get("email") else None,
            "linkedin": p.get("linkedin"), "status": p.get("status") or "unknown",
            "_seniority": _SENIORITY.get(p.get("seniority") or "", 3), "_decider": bool(p.get("decision_maker")),
            "_confidence": p.get("confidence") or 0,
        }
    for p in site_people:
        key = p["name"].lower()
        if key in people:
            people[key]["title"] = people[key]["title"] or p.get("title") or ""
        else:
            people[key] = {"name": p["name"], "title": p.get("title") or "", "email": None, "email_source": None, "linkedin": None,
                           "status": "unknown", "_seniority": 3, "_decider": False, "_confidence": 0}

    matched, others = [], []
    for order, p in enumerate(people.values()):
        r = role_rank(p["title"], roles)
        if r is not None:
            matched.append((r, p["_seniority"], -p["_confidence"], order, p))
        elif p["_decider"] or p["_seniority"] == 0:
            others.append((p["_seniority"], -p["_confidence"], order, p))
    picked = [x[-1] for x in sorted(matched)] or [x[-1] for x in sorted(others)]
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in picked[:limit]]


def guess_email(pattern: str | None, name: str, domain: str) -> str | None:
    """Hunter's email pattern for the company ("{first}.{last}", "{f}{last}"…) filled with this person's name"""
    if not pattern:
        return None
    first, last = (re.sub(r"[^a-z]", "", w.lower()) for w in split_name(name))
    if not first or not last:
        return None
    local = pattern.replace("{first}", first).replace("{last}", last).replace("{f}", first[0]).replace("{l}", last[0])
    if "{" in local or not re.fullmatch(r"[a-z0-9._-]+", local):
        return None
    return f"{local}@{domain}"
