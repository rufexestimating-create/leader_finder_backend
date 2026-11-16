"""Companies from OpenStreetMap (free, no key): the city's area from Nominatim, then the businesses of this kind inside
it that list a website, from the Overpass API. Data © OpenStreetMap contributors (ODbL).

OpenStreetMap is thinner than Google Maps, but it holds companies Google ranks low, so it adds new ones to a search.
"""
import logging
import re

import httpx

log = logging.getLogger("uvicorn.error")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
HEADERS = {"User-Agent": "ReachAgent/1.0 (business lead research)"}

_TRADES = "carpenter|roofer|hvac|electrician|plumber|painter|tiler|plasterer|floorer|stonemason|glaziery|insulation|" \
          "scaffolder|metal_construction|window_construction|builder"
_BUILDERS = ['nwr["craft"="builder"]', 'nwr["office"="construction_company"]']
# Company type -> the OpenStreetMap tags its businesses carry
_FILTERS = {
    "gc": _BUILDERS,
    "cm": _BUILDERS,
    "design_build": _BUILDERS + ['nwr["office"="architect"]'],
    "home_builder": _BUILDERS,
    "remodeler": _BUILDERS + ['nwr["shop"~"^(kitchen|bathroom_furnishing)$"]', 'nwr["craft"~"^(carpenter|tiler|floorer)$"]'],
    "sub": [f'nwr["craft"~"^({_TRADES})$"]'],
    "architect": ['nwr["office"="architect"]'],
    "engineer": ['nwr["office"="engineer"]'],
    "interior": ['nwr["shop"="interior_decoration"]', 'nwr["office"="interior_design"]', 'nwr["craft"="interior_decorator"]'],
    "developer": ['nwr["office"~"property_developer|property_management"]'],
    "supplier": ['nwr["shop"~"doityourself|hardware|trade|building_materials|tiles|flooring|doors|windows"]'],
}


class OsmError(Exception):
    pass


def _area_id(client: httpx.Client, place: str) -> int | None:
    res = client.get(NOMINATIM, params={"q": place, "format": "json", "limit": 1, "featureType": "city"})
    res.raise_for_status()
    hits = [h for h in res.json() if h.get("osm_type") in ("relation", "way")]
    if not hits:
        return None
    osm_id = int(hits[0]["osm_id"])
    return osm_id + (3_600_000_000 if hits[0]["osm_type"] == "relation" else 2_400_000_000)


def search(company_type: str | None, place: str, limit: int = 60) -> list[dict]:
    """[{name, website, phone, address, email, osm_url}] for businesses of this type in `place` that list a website"""
    filters = _FILTERS.get(company_type or "")
    if not filters:
        return []
    try:
        with httpx.Client(timeout=httpx.Timeout(70.0, connect=10.0), headers=HEADERS) as client:
            area = _area_id(client, place)
            if area is None:
                return []
            body = "".join(f"{f}(area.a);" for f in filters)
            query = f"[out:json][timeout:50];area({area})->.a;({body});out tags center {max(limit * 3, 100)};"
            elements = None
            for server in OVERPASS:  # the main server is often busy (502/504): its mirror answers the same query
                try:
                    res = client.post(server, data={"data": query})
                    res.raise_for_status()
                    elements = res.json().get("elements", [])
                    break
                except (httpx.HTTPError, ValueError) as e:
                    log.info("overpass %s failed: %s", server, type(e).__name__)
            if elements is None:
                raise OsmError("OpenStreetMap couldn't be searched right now")
    except (httpx.HTTPError, ValueError) as e:
        log.info("openstreetmap search failed for %r: %s: %s", place, type(e).__name__, e)
        raise OsmError("OpenStreetMap couldn't be searched right now")

    out, seen = [], set()
    for el in elements:
        t = el.get("tags") or {}
        site = (t.get("website") or t.get("contact:website") or t.get("url") or "").strip()
        name = (t.get("name") or "").strip()
        # Restaurants, shops and hotels named "… Kitchen" are not building companies
        if not site or not name or name.lower() in seen or any(k in t for k in ("amenity", "tourism", "leisure")):
            continue
        seen.add(name.lower())
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        address = ", ".join(x for x in (street, t.get("addr:city"), t.get("addr:state"), t.get("addr:postcode")) if x)
        email = (t.get("email") or t.get("contact:email") or "").strip().lower()
        out.append({
            "name": name, "website": site if re.match(r"^https?://", site) else f"https://{site}",
            "phone": (t.get("phone") or t.get("contact:phone") or "").strip() or None,
            "address": address or None, "email": email or None,
            "osm_url": f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
        })
        if len(out) >= limit:
            break
    return out
