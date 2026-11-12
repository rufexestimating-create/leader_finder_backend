from datetime import datetime, timedelta, timezone

import resend
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_one_time_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.supabase import get_supabase
from app.services import email_layout
from app.schemas.auth import SignupRequest, TokenResponse, UserOut

VERIFY_EMAIL = "verify_email"
RESET_PASSWORD = "reset_password"

USER_COLUMNS = "id, email, full_name, is_verified, created_at, password_changed_at"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def to_user_out(row: dict) -> UserOut:
    return UserOut(
        id=row["id"],
        email=row["email"],
        full_name=row.get("full_name"),
        is_verified=row["is_verified"],
        created_at=row["created_at"],
    )


class AuthService:
    # ---------- Supabase helpers ----------

    def get_user_by_email(self, email: str, with_password: bool = False) -> dict | None:
        columns = USER_COLUMNS + (", password_hash" if with_password else "")
        res = get_supabase().table("users").select(columns).eq("email", email.lower()).limit(1).execute()
        return res.data[0] if res.data else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        res = get_supabase().table("users").select(USER_COLUMNS).eq("id", user_id).limit(1).execute()
        return res.data[0] if res.data else None

    def _issue_token(self, user_id: str, purpose: str, lifetime: timedelta) -> str:
        db = get_supabase()
        # Only the newest link of each kind stays valid
        db.table("auth_tokens").delete().eq("user_id", user_id).eq("purpose", purpose).is_("used_at", "null").execute()
        raw, token_hash = generate_one_time_token()
        db.table("auth_tokens").insert({
            "user_id": user_id,
            "token_hash": token_hash,
            "purpose": purpose,
            "expires_at": (_now() + lifetime).isoformat(),
        }).execute()
        return raw

    def _consume_token(self, raw_token: str, purpose: str) -> str:
        """Marks a one-time token as used and returns its user_id, or raises 400."""
        db = get_supabase()
        res = (
            db.table("auth_tokens")
            .select("id, user_id, expires_at, used_at")
            .eq("token_hash", hash_token(raw_token))
            .eq("purpose", purpose)
            .limit(1)
            .execute()
        )
        row = res.data[0] if res.data else None
        if not row or row["used_at"] or datetime.fromisoformat(row["expires_at"]) < _now():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired. Please request a new one.")

        # Conditional update so the same token can't be used twice concurrently
        claimed = (
            db.table("auth_tokens")
            .update({"used_at": _now().isoformat()})
            .eq("id", row["id"])
            .is_("used_at", "null")
            .execute()
        )
        if not claimed.data:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has already been used.")
        return row["user_id"]

    # ---------- Emails ----------

    def _send_auth_email(self, to: str, subject: str, heading: str, body: str, link: str, button: str):
        if not (settings.RESEND_API_KEY.strip() and not settings.RESEND_API_KEY.startswith("re_your_")):
            # No Resend key yet: print the link so the flow can still be tested locally
            print(f"\n[auth email - RESEND_API_KEY not set] To: {to}\n  {subject}\n  {link}\n", flush=True)
            return

        resend.api_key = settings.RESEND_API_KEY
        html = email_layout.account_email(heading=heading, body=body, button_label=button, link=link)
        try:
            resend.Emails.send({"from": settings.DEFAULT_FROM_EMAIL, "to": [to], "subject": subject, "html": html})
        except Exception as e:
            # Don't fail signup/reset if the email provider errors; the user can request another link
            print(f"[auth email] Resend error sending to {to}: {e}\n  link: {link}", flush=True)

    def _send_verification(self, user: dict):
        raw = self._issue_token(user["id"], VERIFY_EMAIL, timedelta(hours=settings.VERIFY_EMAIL_TOKEN_EXPIRE_HOURS))
        link = f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?token={raw}"
        self._send_auth_email(
            user["email"],
            "Verify your email address",
            f"Welcome{', ' + user['full_name'] if user.get('full_name') else ''}!",
            f"Confirm your email to activate your account. This link expires in {settings.VERIFY_EMAIL_TOKEN_EXPIRE_HOURS} hours.",
            link,
            "Verify email",
        )

    # ---------- Flows ----------

    def signup(self, data: SignupRequest) -> UserOut:
        db = get_supabase()
        email = data.email.lower()
        if self.get_user_by_email(email):
            raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

        res = db.table("users").insert({
            "email": email,
            "full_name": data.full_name.strip(),
            "password_hash": hash_password(data.password),
        }).execute()
        user = res.data[0]
        self._send_verification(user)
        return to_user_out(user)

    def login(self, email: str, password: str) -> TokenResponse:
        user = self.get_user_by_email(email, with_password=True)
        # verify_password runs even for unknown emails so timing doesn't leak which accounts exist
        if not verify_password(password, user["password_hash"] if user else None) or not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")
        if not user["is_verified"]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {"code": "email_not_verified", "message": "Please verify your email before logging in."},
            )
        return TokenResponse(
            access_token=create_access_token(user["id"], user["email"]),
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=to_user_out(user),
        )

    def verify_email(self, raw_token: str):
        user_id = self._consume_token(raw_token, VERIFY_EMAIL)
        get_supabase().table("users").update({"is_verified": True, "updated_at": _now().isoformat()}).eq("id", user_id).execute()

    def resend_verification(self, email: str):
        user = self.get_user_by_email(email)
        if user and not user["is_verified"]:
            self._send_verification(user)

    def forgot_password(self, email: str):
        user = self.get_user_by_email(email)
        if not user:
            return  # Same response either way, so the endpoint can't be used to discover accounts
        raw = self._issue_token(user["id"], RESET_PASSWORD, timedelta(minutes=settings.RESET_PASSWORD_TOKEN_EXPIRE_MINUTES))
        self._send_auth_email(
            user["email"],
            "Reset your password",
            "Password reset requested",
            f"Click below to choose a new password. This link expires in {settings.RESET_PASSWORD_TOKEN_EXPIRE_MINUTES} minutes. If you didn't ask for this, ignore this email.",
            f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={raw}",
            "Reset password",
        )

    def reset_password(self, raw_token: str, new_password: str):
        user_id = self._consume_token(raw_token, RESET_PASSWORD)
        now = _now().isoformat()
        # Changing password_changed_at invalidates every JWT issued before now (see get_current_user).
        # Clicking the emailed link also proves the user owns the address, so mark it verified.
        get_supabase().table("users").update({
            "password_hash": hash_password(new_password),
            "password_changed_at": now,
            "is_verified": True,
            "updated_at": now,
        }).eq("id", user_id).execute()


auth_service = AuthService()
