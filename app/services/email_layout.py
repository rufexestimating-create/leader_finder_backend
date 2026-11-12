"""Branded, email-client-safe HTML layout shared by campaign and account emails.

Email clients (Gmail, Outlook, Apple Mail) ignore most modern CSS, so this uses table layout and inline styles only.
"""
import re
from datetime import datetime
from html import escape

from app.core.config import settings

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
TEXT_COLOR = "#1f2937"
MUTED = "#6b7280"
URL_RE = re.compile(r"(https?://[^\s<]+[^\s<.,;:!?)\]'\"])")
BULLET_RE = re.compile(r"^\s*[-*•]\s+")


def brand_name(from_email: str | None = None) -> str:
    """'Arvistry Team <hello@x.com>' -> 'Arvistry Team'; falls back to BRAND_NAME from .env."""
    match = re.match(r'^\s*"?([^"<]+?)"?\s*<', from_email or "")
    return (match.group(1).strip() if match else "") or settings.BRAND_NAME


def _linkify(escaped: str) -> str:
    color = settings.BRAND_COLOR
    return URL_RE.sub(lambda m: f'<a href="{m.group(1)}" style="color:{color};text-decoration:underline">{m.group(1)}</a>', escaped)


def text_to_html(text: str) -> str:
    """Plain text -> nicely spaced paragraphs, bullet lists and clickable links."""
    p_style = f"margin:0 0 16px;font-size:16px;line-height:1.65;color:{TEXT_COLOR}"
    ul_style = f"margin:0 0 16px;padding-left:22px;font-size:16px;line-height:1.6;color:{TEXT_COLOR}"
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        # Within a paragraph, consecutive "- item" lines become a list and other lines stay text
        groups: list[tuple[bool, list[str]]] = []
        for line in (l for l in block.split("\n") if l.strip()):
            is_item = bool(BULLET_RE.match(line))
            if groups and groups[-1][0] == is_item:
                groups[-1][1].append(line)
            else:
                groups.append((is_item, [line]))
        for i, (is_item, lines) in enumerate(groups):
            if is_item:
                items = "".join(f'<li style="margin:0 0 8px;padding-left:4px">{_linkify(escape(BULLET_RE.sub("", l)))}</li>' for l in lines)
                out.append(f'<ul style="{ul_style}">{items}</ul>')
            else:
                tight = i + 1 < len(groups)  # a line introducing a list sits close to it
                style = p_style.replace("margin:0 0 16px", "margin:0 0 8px") if tight else p_style
                out.append(f'<p style="{style}">' + "<br>".join(_linkify(escape(l)) for l in lines) + "</p>")
    return "".join(out)


def button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px"><tr>'
        f'<td style="border-radius:10px;background:{settings.BRAND_COLOR}">'
        f'<a href="{escape(url)}" style="display:inline-block;padding:14px 28px;font-family:{FONT};font-size:16px;font-weight:600;'
        f'color:#ffffff;text-decoration:none;border-radius:10px">{escape(label)}</a>'
        "</td></tr></table>"
    )


def wrap(content_html: str, *, brand: str, preheader: str = "", footer_note: str = "") -> str:
    """Puts the email content inside the branded layout."""
    color = settings.BRAND_COLOR
    initial = escape((brand.strip()[:1] or "•").upper())
    preheader = escape(re.sub(r"\s+", " ", preheader).strip()[:140])
    footer_note = footer_note or f"You're receiving this email from {escape(brand)}."
    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{escape(brand)}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f8;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{preheader}{"&#847;&zwnj;&nbsp;" * 110}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f3f4f8">
  <tr><td align="center" style="padding:32px 16px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px">
      <tr><td style="padding:0 4px 20px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="width:36px;height:36px;border-radius:10px;background:{color};color:#ffffff;font-family:{FONT};font-size:17px;font-weight:700;text-align:center;vertical-align:middle">{initial}</td>
          <td style="padding-left:12px;font-family:{FONT};font-size:17px;font-weight:700;color:#111827">{escape(brand)}</td>
        </tr></table>
      </td></tr>
      <tr><td style="background:#ffffff;border-radius:16px;border-top:4px solid {color};box-shadow:0 1px 3px rgba(17,24,39,0.06)">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="padding:36px 40px 20px;font-family:{FONT};font-size:16px;line-height:1.65;color:{TEXT_COLOR}">{content_html}</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:24px 8px 0;text-align:center;font-family:{FONT};font-size:13px;line-height:1.6;color:{MUTED}">
        {footer_note}<br>&copy; {year} {escape(brand)}. All rights reserved.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def simple_wrap(content_html: str, *, brand: str, preheader: str = "") -> str:
    """A plain, personal-looking email: white page, left-aligned text, no logo, card, colour bar or copyright block.
    Reads like a message typed in Gmail, which is easier to read and is less likely to be filed under Promotions."""
    preheader = escape(re.sub(r"\s+", " ", preheader).strip()[:140])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{escape(brand)}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{preheader}{"&#847;&zwnj;&nbsp;" * 110}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff">
  <tr><td style="padding:24px 20px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px">
      <tr><td style="font-family:{FONT};font-size:15px;line-height:1.6;color:{TEXT_COLOR}">{content_html}</td></tr>
      <tr><td style="padding-top:20px;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED}">Sent by {escape(brand)}</td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def campaign_email(body: str, content_type: str, *, brand: str) -> str:
    """A campaign email body (already personalised) in the plain layout (simple_wrap)."""
    if content_type == "html":
        content, preview_text = body, re.sub(r"<[^>]+>", " ", body)
    else:
        content, preview_text = text_to_html(body).replace("font-size:16px", "font-size:15px"), body
    return simple_wrap(content, brand=brand, preheader=preview_text)


def account_email(*, heading: str, body: str, button_label: str, link: str, note: str = "") -> str:
    """Verification / password reset emails."""
    content = (
        f'<h1 style="margin:0 0 14px;font-size:24px;line-height:1.3;font-weight:700;color:#111827">{escape(heading)}</h1>'
        f'<p style="margin:0 0 24px;font-size:16px;line-height:1.65;color:{TEXT_COLOR}">{escape(body)}</p>'
        + button(button_label, link)
        + f'<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:{MUTED}">Button not working? Paste this link into your browser:<br>'
        f'<a href="{escape(link)}" style="color:{settings.BRAND_COLOR};word-break:break-all">{escape(link)}</a></p>'
    )
    return wrap(
        content,
        brand=settings.BRAND_NAME,
        preheader=body,
        footer_note=note or "If you didn't ask for this email, you can safely ignore it.",
    )
