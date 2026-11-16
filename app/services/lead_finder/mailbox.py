"""Each user's own mailbox (e.g. Hostinger) that ReachAgent sends from.

Emails go out over the mailbox's own SMTP login, so they come from the user's address and replies land in its inbox.
A copy of each one is put in the mailbox's Sent folder over IMAP, so it shows in webmail like any other sent email.
The password is encrypted with the same secret as the tool keys and is never logged.
"""
import imaplib
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.db.supabase import get_supabase
from app.services.lead_finder import keys

TABLE = "prospect_mailboxes"
TIMEOUT = 25
SENT_FALLBACKS = ("INBOX.Sent", "Sent", "Sent Items", "Sent Messages", "INBOX.Sent Items")
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')

log = logging.getLogger("uvicorn.error")


class MailboxError(Exception):
    """The mailbox couldn't be reached or refused the login; nothing was sent"""


# ---------- Storage ----------

def _row(user_id: str) -> dict | None:
    try:
        rows = get_supabase().table(TABLE).select("*").eq("user_id", user_id).limit(1).execute().data
    except Exception as e:
        log.warning("mailbox could not be read: %s", type(e).__name__)
        raise keys.KeysUnavailable("The mailbox storage isn't ready yet: run backend/migrations/003_mailbox_and_sending.sql in Supabase.")
    return rows[0] if rows else None


def status(user_id: str) -> dict:
    """What Settings shows: never the password"""
    r = _row(user_id)
    if not r:
        return {"connected": False}
    return {"connected": True, "email": r["email"], "from_name": r.get("from_name") or "", "smtp_host": r["smtp_host"],
            "smtp_port": r["smtp_port"], "imap_host": r.get("imap_host") or "", "imap_port": r.get("imap_port") or 993,
            "test_mode": is_test(r), "test_to": test_address(r),
            "send_host": r.get("send_host") or "", "send_port": r.get("send_port") or 587,
            "send_user": r.get("send_user") or ""}


def is_test(r: dict) -> bool:
    """Only an explicit off sends to real people; a missing value (column not added yet) stays safe"""
    return r.get("test_mode") is not False


def test_address(r: dict) -> str:
    """Where test mode sends every email: the inbox chosen in Settings, else the mailbox itself"""
    return (r.get("test_to") or "").strip() or r["email"]


def set_test_to(user_id: str, address: str) -> None:
    try:
        get_supabase().table(TABLE).update({"test_to": address or None}).eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("test inbox could not be saved: %s", type(e).__name__)
        raise keys.KeysUnavailable("The test inbox couldn't be saved: run backend/migrations/006_mailbox_test_inbox.sql in Supabase.")


def set_test_mode(user_id: str, on: bool) -> None:
    try:
        get_supabase().table(TABLE).update({"test_mode": on}).eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("test mode could not be saved: %s", type(e).__name__)
        raise keys.KeysUnavailable("Test mode couldn't be saved: run backend/migrations/004_mailbox_test_mode.sql in Supabase.")


def load(user_id: str) -> dict | None:
    """The mailbox with its password readable, or None when none is saved (or it can't be decrypted)"""
    r = _row(user_id)
    password = keys.decrypt(r["password_encrypted"]) if r else None
    if not password:
        return None
    mb = {**r, "password": password}
    if r.get("send_pass_encrypted"):
        send_pass = keys.decrypt(r["send_pass_encrypted"])
        if send_pass:
            mb["send_pass"] = send_pass
    return mb


def save(user_id: str, mb: dict) -> None:
    row = {
        "user_id": user_id, "email": mb["email"], "from_name": mb.get("from_name") or None,
        "smtp_host": mb["smtp_host"], "smtp_port": mb["smtp_port"], "imap_host": mb.get("imap_host") or None,
        "imap_port": mb.get("imap_port"), "password_encrypted": keys.encrypt(mb["password"]),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        get_supabase().table(TABLE).upsert(row, on_conflict="user_id").execute()
    except Exception as e:
        log.warning("mailbox could not be saved: %s", type(e).__name__)
        raise keys.KeysUnavailable("The mailbox couldn't be saved. Run backend/migrations/003_mailbox_and_sending.sql in Supabase if you haven't.")


def remove(user_id: str) -> None:
    try:
        get_supabase().table(TABLE).delete().eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("mailbox could not be removed: %s", type(e).__name__)
        raise keys.KeysUnavailable("The mailbox couldn't be removed.")


def save_send_smtp(user_id: str, data: dict) -> None:
    """Save the external sending server (e.g. Brevo). Password encrypted like the mailbox password."""
    row = {"send_host": data["send_host"], "send_port": data["send_port"],
           "send_user": data["send_user"], "send_pass_encrypted": keys.encrypt(data["send_pass"])}
    try:
        get_supabase().table(TABLE).update(row).eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("send smtp could not be saved: %s", type(e).__name__)
        raise keys.KeysUnavailable("The sending server couldn't be saved: run backend/migrations/007_brevo_smtp.sql in Supabase.")


def remove_send_smtp(user_id: str) -> None:
    try:
        get_supabase().table(TABLE).update(
            {"send_host": None, "send_port": None, "send_user": None, "send_pass_encrypted": None}
        ).eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("send smtp could not be removed: %s", type(e).__name__)
        raise keys.KeysUnavailable("The sending server couldn't be removed.")


# ---------- Connecting ----------

def _smtp(mb: dict) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    # Use external sending server (e.g. Brevo) when configured; otherwise use the mailbox's own SMTP
    host = mb.get("send_host") or mb["smtp_host"]
    port = int(mb.get("send_port") or mb["smtp_port"])
    user = mb.get("send_user") or mb["email"]
    pw   = mb.get("send_pass") or mb["password"]
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT)
    else:
        s = smtplib.SMTP(host, port, timeout=TIMEOUT)
        s.starttls(context=ctx)
    s.login(user, pw)
    return s


def check_send_smtp(data: dict) -> keys.Check:
    """Test the external sending server credentials. Never raises."""
    where = f"{data['send_host']}:{data['send_port']}"
    mb = {"smtp_host": data["send_host"], "smtp_port": data["send_port"],
          "email": data["send_user"], "password": data["send_pass"]}
    try:
        with _smtp(mb):
            pass
    except smtplib.SMTPAuthenticationError:
        return keys.Check(False, f"The sending server at {where} didn't accept this login. Check the SMTP login and key.")
    except (OSError, smtplib.SMTPException):
        return keys.Check(False, f"Couldn't connect to {where}. Check the host and port.")
    return keys.Check(True, f"Connected to {where}. Emails will go out through it.")


def _imap(mb: dict) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(mb["imap_host"], int(mb.get("imap_port") or 993), ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    m.login(mb["email"], mb["password"])
    return m


def check(mb: dict) -> keys.Check:
    """Logs in to sending (SMTP) and, when given, receiving (IMAP). Never raises, never logs the password."""
    where = f"{mb['smtp_host']}:{mb['smtp_port']}"
    try:
        with _smtp(mb):
            pass
    except smtplib.SMTPAuthenticationError:
        return keys.Check(False, "The mailbox didn't accept this email and password. Check them in your Hostinger email settings.")
    except (OSError, smtplib.SMTPException) as e:
        return keys.Check(False, f"Couldn't connect to the outgoing server {where}. Check the SMTP server and port.")
    if mb.get("imap_host"):
        try:
            with _imap(mb):
                pass
        except imaplib.IMAP4.error:
            return keys.Check(False, "Sending works, but the incoming (IMAP) login failed, so sent emails can't be copied to your Sent folder.")
        except OSError:
            return keys.Check(False, f"Couldn't connect to the incoming server {mb['imap_host']}. Check the IMAP server and port.")
    return keys.Check(True, f"Connected. Emails go out from {mb['email']} and replies arrive in its inbox.")


# ---------- Writing and sending ----------

def render(settings: dict, company: dict, contact: dict) -> tuple[str, str]:
    """Subject and body exactly as the email preview shows them"""
    # A company address (info@) is greeted as the team, not by the company's first word
    first = (f"{company.get('name') or ''} team" if contact.get("title") == "Company email"
             else (contact.get("name") or "").split(" ")[0])
    line = (contact.get("personal_line") or "").strip()

    def fill(text: str) -> str:
        text = re.sub(r"\{\{\s*first_name\s*\}\}", lambda _: first, text)
        text = re.sub(r"\{\{\s*company\s*\}\}", lambda _: company.get("name") or "", text)
        return re.sub(r"\{\{\s*personal_line\s*\}\}\n*", lambda _: f"{line}\n\n" if line else "", text)

    return fill(settings.get("subject") or ""), fill(settings.get("message") or "").strip()


def build(mb: dict, to_email: str, to_name: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mb.get("from_name") or "", mb["email"]))
    msg["To"] = formataddr((to_name, to_email))
    msg["Subject"] = subject
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=mb["email"].rsplit("@", 1)[-1])
    msg.set_content(body)
    return msg


def _sent_folder(m: imaplib.IMAP4_SSL) -> str | None:
    _, folders = m.list()
    names = []
    for raw in folders or []:
        match = _LIST_RE.match(raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw))
        if not match:
            continue
        name = match["name"].strip().strip('"')
        if "\\Sent" in match["flags"]:
            return name
        names.append(name)
    return next((n for n in SENT_FALLBACKS if n in names), None)


def _copy_to_sent(mb: dict, msgs: list[EmailMessage]) -> None:
    """Best effort: the emails already went out, so a failure here is only logged"""
    try:
        with _imap(mb) as m:
            folder = _sent_folder(m)
            if not folder:
                return
            for msg in msgs:
                m.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    except Exception as e:
        log.warning("sent emails couldn't be copied to the Sent folder: %s", type(e).__name__)


def _reason(e: smtplib.SMTPException) -> str:
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The mail server refused this address."
    text = getattr(e, "smtp_error", b"")
    text = text.decode(errors="replace") if isinstance(text, bytes) else str(text or e)
    return f"The mail server didn't send it: {text[:160]}"


def send(mb: dict, msgs: list[EmailMessage], copy_to_sent: bool = True) -> list[str | None]:
    """Sends each email over one connection. Returns, for each one, None when it went out or why it didn't.
    Raises MailboxError when the mailbox can't be reached at all, so nothing was sent."""
    try:
        conn = _smtp(mb)
    except smtplib.SMTPAuthenticationError:
        raise MailboxError("Your mailbox didn't accept its saved password. Update it in Settings, under Your mailbox.")
    except (OSError, smtplib.SMTPException):
        raise MailboxError(f"Couldn't connect to your mailbox's server {mb['smtp_host']}. Try again in a moment.")
    errors: list[str | None] = []
    with conn:
        for msg in msgs:
            try:
                conn.send_message(msg)
                errors.append(None)
            except smtplib.SMTPServerDisconnected:
                errors.append("The mail server closed the connection. Try sending again.")
                errors.extend(["The mail server closed the connection. Try sending again."] * (len(msgs) - len(errors)))
                break
            except smtplib.SMTPException as e:
                errors.append(_reason(e))
    sent = [m for m, err in zip(msgs, errors) if err is None]
    if sent and copy_to_sent and mb.get("imap_host"):
        _copy_to_sent(mb, sent)
    return errors
