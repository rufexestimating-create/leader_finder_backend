"""
Repairs emails that directories store with other contact text glued on, e.g.
  n8y4b8canada519-551-4117robert@abrik.com          -> robert@abrik.com
  94121stephen@duffinandco.com                      -> stephen@duffinandco.com
  x@outlook.comwww.site.org                         -> x@outlook.com
  http//corporate@honeyelectric.com, %20pr@dpr.com  -> corporate@..., pr@...
Returns "" when what is left is not a usable address.
"""
import re
from urllib.parse import unquote

_VALID = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")
_PHONE = re.compile(r"\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}")
# A zip code or partial phone glued in front: a run of digits/separators holding 4+ digits, before letters
_LEAD_NUMBER = re.compile(r"^[\d\-.() ]*\d[\d\-.() ]*(?=[a-z])")
_URL_PREFIX = re.compile(r"^(?:mailto:|https?:?/*|www\.)+")


def fix_email(raw: str) -> str:
    e = unquote(raw or "").strip().lower()
    e = e.strip(" \t\r\n<>\"'()[]{},;:")
    e = _URL_PREFIX.sub("", e)
    if e.count("@") != 1:
        return ""
    local, domain = e.split("@")

    domain = domain.removeprefix("www.")
    domain = re.split(r"www\.|https?:", domain)[0].rstrip(".-")

    # Keep what follows the last phone number; a mailbox that IS a phone number stays as it is
    phones = list(_PHONE.finditer(local))
    if phones and local[phones[-1].end():].strip("._-"):
        local = local[phones[-1].end():]
    lead = _LEAD_NUMBER.match(local)
    if lead and sum(ch.isdigit() for ch in lead.group()) >= 4:
        local = local[lead.end():]
    local = local.strip("._-")

    fixed = f"{local}@{domain}"
    return fixed if local and _VALID.fullmatch(fixed) else ""
