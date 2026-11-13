"""Finding a person's work email without any paid tool: guess it from their name, then ask the company's own mail
server whether that mailbox exists (SMTP RCPT, no email is sent).

What testing showed and is handled here:
  * port 25 over IPv6 hangs on our server, so we always connect over IPv4
  * Microsoft 365 answers "too many recipients" after 2 addresses per connection, so every guess gets its own connection
  * Google's mail servers don't accept our connections, so those domains are reported as unverifiable, never guessed
  * a domain that accepts any address (catch-all) can't confirm a guess, so nothing is claimed for it
"""
import logging
import random
import re
import smtplib
import socket
import string
import time
from dataclasses import dataclass, field

import dns.resolver

log = logging.getLogger("uvicorn.error")

HELO = "leadfinder.187-124-96-140.sslip.io"
TIMEOUT = 15
MAX_GUESSES = 8
UNREACHABLE_MX = ("google.com", "googlemail.com")

# Most common business patterns first
PATTERNS = ["{f}.{l}", "{fi}{l}", "{f}", "{f}{l}", "{fi}.{l}", "{f}{li}", "{f}_{l}", "{l}", "{l}.{f}", "{l}{fi}", "{f}-{l}", "{f}.{li}"]
FREE_MAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "live.com", "msn.com", "me.com")
ROLE_WORDS = ("info", "office", "contact", "hello", "sales", "admin", "estimating", "estimates", "bids", "projects", "service",
              "support", "team", "mail", "inquiries", "inquiry", "general")


@dataclass
class DomainCheck:
    mx: str | None = None
    verifiable: bool = False     # we can ask this server about mailboxes
    catch_all: bool = False
    reason: str = ""
    cache: dict = field(default_factory=dict)


def split_name(name: str) -> tuple[str, str]:
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in re.sub(r"\b[A-Z]\.\s*", "", name).split()]
    parts = [p for p in parts if p and p not in ("jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr")]
    return (parts[0], parts[-1]) if len(parts) >= 2 else ("", "")


def _fill(pattern: str, name: str, domain: str) -> str | None:
    f, l = split_name(name)
    if not f or not l:
        return None
    return pattern.format(f=f, l=l, fi=f[0], li=l[0]) + "@" + domain


def guesses(name: str, domain: str, pattern: str | None = None) -> list[str]:
    out = [_fill(p, name, domain) for p in ([pattern] if pattern else PATTERNS)]
    return [e for e in dict.fromkeys(out) if e][:MAX_GUESSES]


def learn_pattern(emails: list[str], people: list[dict], domain: str) -> str | None:
    """A named person's email on the website shows the company's pattern, e.g. jane.doe@ -> {f}.{l}"""
    own = set(emails)
    for p in people:
        for pattern in PATTERNS:
            if _fill(pattern, p["name"], domain) in own:
                return pattern
    return None


def owner_of(email: str, people: list[dict], domain: str) -> dict | None:
    for p in people:
        if any(_fill(pattern, p["name"], domain) == email for pattern in PATTERNS):
            return p
    return None


def is_role(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local == w or local.startswith(w + ".") or local.startswith(w + "-") for w in ROLE_WORDS)


def _mx(domain: str) -> str | None:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=10)
        return str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".").lower()
    except Exception:
        return None


def _ipv4(host: str) -> str:
    return socket.getaddrinfo(host, 25, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


def _ask(mx: str, email: str) -> int:
    """The server's answer code for one address, on its own connection"""
    with smtplib.SMTP(_ipv4(mx), 25, timeout=TIMEOUT, local_hostname=HELO) as s:
        s.ehlo(HELO)
        s.mail("")
        return s.rcpt(email)[0]


def check_domain(domain: str) -> DomainCheck:
    d = DomainCheck(mx=_mx(domain))
    if not d.mx:
        d.reason = "The domain doesn't receive email"
        return d
    if d.mx.endswith(UNREACHABLE_MX):
        d.reason = "Google mail can't be checked from our server"
        return d
    probe = "zz" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + "@" + domain
    try:
        code = _ask(d.mx, probe)
    except (OSError, smtplib.SMTPException) as e:
        d.reason = f"The mail server didn't answer ({type(e).__name__})"
        return d
    if code == 250:
        d.catch_all, d.reason = True, "The domain accepts any address, so a guess can't be confirmed"
        return d
    if code >= 500:
        d.verifiable = True
    else:
        d.reason = "The mail server asked us to try later"
    return d


def mailbox_exists(d: DomainCheck, email: str) -> bool:
    if not d.verifiable:
        return False
    if email not in d.cache:
        try:
            code = _ask(d.mx, email)
            d.cache[email] = code == 250
            if 400 <= code < 500:  # throttled: stop asking this server
                d.verifiable = False
                d.reason = "The mail server asked us to slow down"
        except (OSError, smtplib.SMTPException):
            d.cache[email] = False
        time.sleep(0.8)
    return d.cache[email]


def find_email(d: DomainCheck, name: str, domain: str, pattern: str | None = None) -> str | None:
    """The guess the company's server confirms, or None"""
    for e in guesses(name, domain, pattern):
        if mailbox_exists(d, e):
            return e
        if not d.verifiable:
            break
    return None
