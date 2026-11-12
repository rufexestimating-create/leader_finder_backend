"""The personal line: the first switched-on template whose details were all found, so a line never says something we
don't know. Same rules as fillTemplate / pickLine in frontend/src/lib/leadFinder.ts."""
import re

from app.services.lead_finder.constants import type_label

_VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# "Saffron Line Architects's" -> "Saffron Line Architects'"
_POSSESSIVE = re.compile(r"(\w)s's\b")


def facts(company: dict) -> dict[str, str | None]:
    """What a template may use, as text; None when it wasn't found"""
    rating = company.get("rating")
    return {
        "company": (company.get("name") or "").strip() or None,
        "city": (company.get("city") or "").strip() or None,
        "industry": type_label(company.get("company_type")).lower() or None,
        "founded_year": str(company["founded_year"]) if company.get("founded_year") else None,
        "rating": f"{float(rating):.1f}" if rating else None,
        "reviews": str(company["reviews"]) if company.get("reviews") else None,
    }


def fill(text: str, values: dict[str, str | None]) -> str | None:
    """Fills {{company}}, {{city}}…; None when any of them has no value, so the template is skipped"""
    missing = False

    def value(m: re.Match) -> str:
        nonlocal missing
        v = values.get(m.group(1))
        if not v:
            missing = True
            return ""
        return v

    out = _VARIABLE.sub(value, text)
    return None if missing else _POSSESSIVE.sub(r"\1s'", out).strip()


def pick_line(company: dict, templates: list[dict]) -> tuple[str, int] | None:
    """(line, template number counted from 1) or None when no template fits"""
    values = facts(company)
    for i, t in enumerate(templates, start=1):
        if not t.get("enabled", True) or not (t.get("text") or "").strip():
            continue
        line = fill(t["text"], values)
        if line:
            return line, i
    return None
