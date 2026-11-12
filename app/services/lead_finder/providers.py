"""Clients for the three outside tools. Each turns the tool's own errors into one sentence a non-technical user can act on.

Contracts (checked against each provider's API docs on 11 November 2026):
  Hunter v2       GET  https://api.hunter.io/v2/domain-search  (domain or company, limit)
                  GET  https://api.hunter.io/v2/email-finder   (domain, first_name, last_name)
                  key in the X-API-KEY header; errors are {"errors": [{"id", "code", "details"}]}
  Serper          POST https://google.serper.dev/search, /places and /news, key in X-API-KEY, body {"q", "gl", "num"}
  Reoon           GET  https://emailverifier.reoon.com/api/v1/verify?email=…&key=…&mode=power; reads `status`;
                  errors are {"status": "error", "reason": …} (401 for a wrong key)
Keys never go into a URL we log: Hunter and Serper take them as headers; Reoon only accepts a query parameter,
so its URL is never logged.
"""
import logging
import time

import httpx

log = logging.getLogger("uvicorn.error")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ProviderError(Exception):
    """A tool couldn't answer. `fatal` = no point trying again today (wrong key, credits used up)."""

    def __init__(self, message: str, fatal: bool = False, code: str | None = None):
        super().__init__(message)
        self.fatal = fatal
        self.code = code  # the tool's own error name, e.g. "invalid_industry"


def _fail(tool: str, res: httpx.Response) -> ProviderError:
    detail, code = "", None
    try:
        body = res.json()
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            detail = errors[0].get("details") or errors[0].get("id") or ""
            code = errors[0].get("id")
        elif isinstance(body, dict):
            detail = body.get("message") or body.get("error") or body.get("reason") or ""
    except ValueError:
        pass
    status = res.status_code
    if status == 401:
        return ProviderError(f"The {tool} API key is wrong. Replace it in Settings, under Tools.", fatal=True, code=code)
    if status == 429 or (status == 400 and "credit" in str(detail).lower()):
        return ProviderError(f"{tool}'s credits for this month are used up.", fatal=True, code=code)
    if status == 403:
        return ProviderError(f"{tool} refused the request ({detail or 'rate limit or plan'}). It will be tried again later.", code=code)
    return ProviderError(f"{tool} answered with an error {status}{f': {detail}' if detail else ''}.", code=code)


def _call(tool: str, send) -> httpx.Response:
    try:
        res = send()
    except httpx.TimeoutException:
        raise ProviderError(f"{tool} didn't answer in time. It will be tried again later.")
    except httpx.HTTPError as e:
        # The tool's own words go to the server log; the user gets one plain sentence
        log.warning("%s could not be reached: %r", tool, e)
        raise ProviderError(f"Couldn't reach {tool}. Check the server's internet connection.")
    if res.status_code >= 400:
        raise _fail(tool, res)
    return res


def _hunter_status(v: str | None) -> str:
    # Hunter: valid | accept_all | unknown
    return {"valid": "valid", "accept_all": "catch_all", "invalid": "invalid"}.get((v or "").lower(), "unknown")


class Hunter:
    BASE = "https://api.hunter.io/v2"

    def __init__(self, api_key: str, client: httpx.Client):
        self.client = client
        self.headers = {"X-API-KEY": api_key}

    def domain_search(self, domain: str | None = None, company: str | None = None, limit: int = 10) -> dict:
        """The company's website (when searched by name), its email pattern and the people Hunter knows there"""
        params: dict = {"limit": limit}
        if domain:
            params["domain"] = domain
        elif company:
            params["company"] = company
        else:
            raise ValueError("domain or company is required")
        res = _call("Hunter", lambda: self.client.get(f"{self.BASE}/domain-search", params=params, headers=self.headers, timeout=TIMEOUT))
        data = (res.json() or {}).get("data") or {}
        people = []
        for e in data.get("emails") or []:
            people.append({
                "email": (e.get("value") or "").lower() or None,
                "type": e.get("type"),
                "first_name": e.get("first_name") or "",
                "last_name": e.get("last_name") or "",
                "title": e.get("position") or e.get("position_raw") or "",
                "seniority": e.get("seniority"),
                "decision_maker": bool(e.get("decision_maker")),
                "linkedin": e.get("linkedin"),
                "confidence": e.get("confidence") or 0,
                "status": _hunter_status((e.get("verification") or {}).get("status")),
            })
        return {
            "domain": (data.get("domain") or "").lower() or None,
            "organization": data.get("organization"),
            "pattern": data.get("pattern"),
            "accept_all": bool(data.get("accept_all")),
            "people": people,
        }

    DISCOVER_MESSAGES = {
        "no_discover_access": "Your Hunter plan doesn't include company search (Discover).",
        "invalid_headcount": "Hunter doesn't know that company size.",
        "invalid_headquarters_location_include_state": "Hunter can't search that state: pick a city instead.",
        "pagination_error": "Hunter can't return that many companies on your plan.",
    }

    def discover(self, body: dict) -> dict:
        """Companies matching the filters (free, no credits): {companies: [{name, domain, emails}], total}"""
        try:
            res = _call("Hunter", lambda: self.client.post(f"{self.BASE}/discover", json=body, headers=self.headers, timeout=TIMEOUT))
        except ProviderError as e:
            if e.code in self.DISCOVER_MESSAGES:
                raise ProviderError(self.DISCOVER_MESSAGES[e.code], fatal=e.fatal, code=e.code)
            raise
        j = res.json() or {}
        return {
            "companies": [
                {"name": d.get("organization") or d.get("domain") or "", "domain": d.get("domain") or "",
                 "emails": int((d.get("emails_count") or {}).get("total") or 0)}
                for d in j.get("data") or [] if d.get("domain")
            ],
            "total": int((j.get("meta") or {}).get("results") or 0),
        }

    def email_finder(self, domain: str, first_name: str, last_name: str) -> dict | None:
        """The most likely email for one person, or None when Hunter has no answer (no credit is used then)"""
        params = {"domain": domain, "first_name": first_name, "last_name": last_name}
        res = _call("Hunter", lambda: self.client.get(f"{self.BASE}/email-finder", params=params, headers=self.headers, timeout=TIMEOUT))
        data = (res.json() or {}).get("data") or {}
        if not data.get("email"):
            return None
        return {
            "email": data["email"].lower(),
            "score": data.get("score") or 0,
            "title": data.get("position") or "",
            "linkedin": data.get("linkedin_url"),
            "status": _hunter_status((data.get("verification") or {}).get("status")),
        }


class Serper:
    BASE = "https://google.serper.dev"

    def __init__(self, api_key: str, client: httpx.Client):
        self.client = client
        self.headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    def _post(self, path: str, body: dict) -> dict:
        res = _call("Serper", lambda: self.client.post(f"{self.BASE}/{path}", json=body, headers=self.headers, timeout=TIMEOUT))
        return res.json() or {}

    def search(self, query: str, country: str | None = None, num: int = 10) -> list[dict]:
        """Google results: [{title, link, snippet}]"""
        body: dict = {"q": query, "num": num}
        if country:
            body["gl"] = country
        return [
            {"title": r.get("title") or "", "link": r.get("link") or "", "snippet": r.get("snippet") or ""}
            for r in self._post("search", body).get("organic") or []
            if r.get("link")
        ]

    def news(self, query: str, country: str | None = None, num: int = 8) -> list[dict]:
        """Google News results: [{title, link, snippet, date, source}]; `date` is text like "3 weeks ago" """
        body: dict = {"q": query, "num": num}
        if country:
            body["gl"] = country
        return [
            {"title": r.get("title") or "", "link": r.get("link") or "", "snippet": r.get("snippet") or "",
             "date": r.get("date"), "source": r.get("source")}
            for r in self._post("news", body).get("news") or []
            if r.get("link")
        ]

    def places(self, query: str, country: str | None = None) -> list[dict]:
        """Google Maps results: [{title, address, rating, reviews, phone, website}]"""
        body: dict = {"q": query}
        if country:
            body["gl"] = country
        return [
            {
                "title": p.get("title") or "",
                "address": p.get("address"),
                "rating": p.get("rating"),
                "reviews": p.get("ratingCount"),
                "phone": p.get("phoneNumber"),
                "website": p.get("website"),
            }
            for p in self._post("places", body).get("places") or []
        ]


class ReoonVerifier:
    URL = "https://emailverifier.reoon.com/api/v1/verify"
    # Power mode asks the mailbox itself, so it can tell catch-all domains apart; it can take a minute
    TIMEOUT = httpx.Timeout(90.0, connect=10.0)
    # Reoon's power-mode `status` -> ours. Role accounts (info@) and full inboxes are real but unreliable, so they count as risky.
    STATUS = {
        "safe": "valid",
        "catch_all": "catch_all",
        "role_account": "catch_all",
        "inbox_full": "catch_all",
        "invalid": "invalid",
        "disabled": "invalid",
        "disposable": "invalid",
        "spamtrap": "invalid",
    }

    def __init__(self, api_key: str, client: httpx.Client):
        self.client = client
        self.api_key = api_key

    def verify(self, email: str) -> str:
        """valid | catch_all | invalid | unknown"""
        params = {"email": email, "key": self.api_key, "mode": "power"}
        send = lambda: self.client.get(self.URL, params=params, timeout=self.TIMEOUT)  # noqa: E731
        try:
            res = _call("Reoon", send)
        except ProviderError as e:
            if e.fatal:
                raise
            time.sleep(1.5)  # a dropped connection is usually gone on the second try
            res = _call("Reoon", send)
        body = res.json() or {}
        if body.get("status") == "error":
            reason = str(body.get("reason") or "").strip()
            if "credit" in reason.lower():
                raise ProviderError("Reoon's credits are used up.", fatal=True)
            raise ProviderError(f"Reoon couldn't check {email}: {reason or 'unknown error'}")
        return self.STATUS.get(str(body.get("status") or "").lower(), "unknown")
